"""MILP-modell (PuLP + HiGHS) för korttidsplanering av en vattenkraftskaskad.

Horisonten delas i tidsblock: kvartar det första dygnet (day-ahead-marknadens upplösning),
timmar det andra och fyratimmarsblock därefter, där planen bara behövs för att värdera
vattnet rätt.

För kraftverk p, aggregat u, block b (längd d_b timmar) och segment k på produktionskurvan:

    max  sum_b d_b * pris_b * sum_p,u P_pub  -  sum startkostnad * start_pub  +  sum_p F_p(V_p,slut)
         - straff * |levererad effekt - budåtagande|   (första dygnet, när buden redan är lagda)

    P_pub = h_p * (Pmin_u * z_pub + sum_k e_k * q_pubk)   effekt (MW), h_p = fallhöjdsfaktor
    Q_pub = Qmin_u * z_pub + sum_k q_pubk                 flöde genom aggregatet
    0 <= q_pubk <= bredd_k * z_pub                        bara flöde när aggregatet är i drift
    start_pub >= z_pub - z_pu(b-1)                        start när aggregatet går från 0 till 1
    V_pb = V_p(b-1) + c * d_b * (lokal tillrinning_pb + ankomst_pb - R_pb)
    R_pb = sum_u Q_pub + spill_pb >= minimitappning_p     total tappning (mjukt villkor, högt straff)
    0 <= V_pb <= Vmax_p,  z binär

Vattenhushållningsbestämmelser för hur fort tappningen får ändras (kraftverk med ramp):

    |R_pb - R_p(b-1)| <= ramp_p * (d_b-1 + d_b) / 2      korttidsreglering med begränsad takt
    R_pb = R_p(b-1)                                     inom samma dygn när korttidsreglering är förbjuden
    |R_pb - R_p(b-1)| <= ändring per dygn               vid dygnsskiftet de dygnen

Båda är mjuka villkor med högt straff, liksom minimitappningen.

Ankomsten är tappningen från kraftverket ovanför, förskjuten med gångtiden och fördelad på
de block den hamnar i. Fallhöjdsfaktorn sätts efter magasinsnivån när planen görs; över en
vecka ändras ett stort magasins nivå lite. F_p är vattenvärdesfunktionen: en konkav,
styckvis linjär funktion av slutvolymen från långtidsplaneringen (se watervalues.py).

Stödtjänster (om priser anges) samoptimeras med spot. För kraftverk p och block b:

    P + FCR-N + FCR-D upp            <= h * Pmax * (aggregat i drift)
    P + FCR-N + FCR-D upp + mFRR upp <= h * Pmax * (alla aggregat)   (vattenkraft startar på minuter)
    P - FCR-N - FCR-D ned            >= h * Pmin * (aggregat i drift)
    P - FCR-N - FCR-D ned - mFRR ned >= 0                             (aggregaten kan stoppas)

FCR begränsas dessutom till en andel av kapaciteten i drift och kaskadens totala reserv per
produkt till en andel av vad elområdet upphandlar. Reserven är konstant inom timmen, som på
marknaden, och betalas per MW och timme. Aktiveringsenergin ingår inte.

optimize_scenarios löser samma modell för flera prisscenarier på en gång (en tvåstegsmodell):
varje scenario har sin egen körplan, men de reserver som säljs innan priset är känt är
gemensamma, produktionen första dygnet ska gå att lägga som en budkurva, och målet är det
förväntade värdet.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pulp

from cascade import MM3_PER_M3S_HOUR, Cascade

# Liten straffkostnad på spill, € per (m³/s·h) och timme kvar av horisonten. När magasinet
# ändå väntas bli fullt är det likgiltigt för en deterministisk modell när spillet sker; då ska
# det ske så sent som möjligt, eftersom tillrinningen kan bli lägre än prognosen och spill inte
# går att ångra. Utan detta spiller en rullande plan i onödan under vårfloden.
SPILL_PENALTY = 0.01
# Brott mot minimitappningen ska i praktiken aldrig ske, men modellen måste alltid ge en plan
# även när utgångsläget gör villkoret omöjligt att uppfylla. € per (m³/s·h) som saknas.
MIN_FLOW_PENALTY = 1000.0
# Samma sak för hur fort tappningen får ändras, € per (m³/s) över gränsen och timme. Högre än
# för minimitappningen: genomförandet (simulation.regulate) följer alltid ändringstakten, så en
# plan som bryter mot den går inte att genomföra som planerad.
RAMP_PENALTY = 10_000.0
# Dammar med minimitappning planeras med en säkerhetsmarginal, så att en tillrinning under
# prognos inte direkt leder till brott mot vattendomen. Mjukt villkor, € per saknad Mm³·h.
SAFETY_MARGIN = 0.15
SAFETY_PENALTY = 10_000.0
# Förväntad kostnad för att avvika från budåtagandet (€/MWh). Obalanspriset kan bli både bättre
# och sämre än spotpriset; i SE3 skilde det i snitt 41 €/MWh åt något håll (okt 2025–okt 2026).
IMBALANCE_PENALTY = 40.0
# Stödtjänster i ordningen FCR-N, FCR-D upp, FCR-D ned, mFRR upp, mFRR ned (som data.RESERVES).
# Hur stor del av kapaciteten i drift ett vattenkraftaggregat kan erbjuda som FCR (antagande).
FCR_N_SHARE = 0.10
FCR_D_SHARE = 0.20
RESERVE_SHORTFALL_PENALTY = 1000.0   # € per MW och timme som inte kan levereras av redan såld reserv
RETRY_SEEDS = (1, 2, 3)   # slumpfrön för HiGHS när en lösning avbryts av ett numeriskt fel


@dataclass
class State:
    """Kaskadens tillstånd vid planeringens start."""
    volume: np.ndarray              # Mm³ per kraftverk
    release_history: np.ndarray     # (perioder, kraftverk): senaste periodernas tappning, m³/s
    units_on: np.ndarray            # antal aggregat i drift perioden innan


@dataclass
class Terminal:
    """Värdet av vattnet i varje magasin vid horisontens slut: en konkav, styckvis linjär
    funktion av volymen. edges är brytpunkter i Mm³ (från 0 till Vmax), slopes är
    marginalvärdet i €/Mm³ för varje segment och får inte öka med volymen."""
    edges: list[np.ndarray]
    slopes: list[np.ndarray]

    def __post_init__(self) -> None:
        for s in self.slopes:
            if np.any(np.diff(s) > 1e-6 * max(1.0, np.abs(s).max())):
                raise ValueError("Vattenvärdesfunktionen måste vara konkav (avtagande marginalvärde)")

    def value(self, volume: np.ndarray) -> float:
        return float(sum(np.sum(s * np.clip(v - e[:-1], 0, np.diff(e)))
                         for v, e, s in zip(volume, self.edges, self.slopes)))

    def marginal(self, p: int, volume: float) -> float:
        """Marginalvärdet (€/Mm³) av det sista vattnet i magasin p vid given volym."""
        j = np.searchsorted(self.edges[p], volume, side="left") - 1
        return float(self.slopes[p][np.clip(j, 0, len(self.slopes[p]) - 1)])

    def scaled(self, factor: float) -> "Terminal":
        return Terminal(self.edges, [s * factor for s in self.slopes])


@dataclass
class Plan:
    """Planen per period (block utvidgade till perioder)."""
    flow: np.ndarray        # (T, P) turbinflöde m³/s
    spill: np.ndarray       # (T, P) förbitappning m³/s
    units: np.ndarray       # (T, P) antal aggregat i drift
    power: np.ndarray       # (T, P) MW
    volume: np.ndarray      # (T, P) Mm³ vid blockets slut
    objective: float
    gap: float              # relativt MIP-gap vid avslut
    reserves: np.ndarray | None = None   # (T, 5) MW per stödtjänst för hela kaskaden


def time_blocks(steps: int, step_h: float = 0.25) -> list[int]:
    """Blocklängder i antal perioder: periodupplösning första dygnet, timmar andra dygnet,
    fyratimmarsblock därefter."""
    per_hour = round(1 / step_h)
    per_day = 24 * per_hour
    blocks = [1] * min(steps, per_day)
    while sum(blocks) < min(steps, 2 * per_day):
        blocks.append(min(per_hour, steps - sum(blocks)))
    while sum(blocks) < steps:
        blocks.append(min(4 * per_hour, steps - sum(blocks)))
    return blocks


def _overlaps(edges: np.ndarray, delay: int, n_history: int) -> list[tuple[dict, dict]]:
    """För varje block: vikter för de uppströmsblock och historikperioder vars tappning
    anländer under blocket (allt i antal perioder). Vikten är överlappets längd delat
    med blockets längd."""
    out = []
    for b in range(len(edges) - 1):
        lo, hi = edges[b] - delay, edges[b + 1] - delay
        length = edges[b + 1] - edges[b]
        from_blocks = {j: (min(hi, edges[j + 1]) - max(lo, edges[j])) / length
                       for j in range(len(edges) - 1) if min(hi, edges[j + 1]) > max(lo, edges[j])}
        from_history = {h: (min(hi, h + 1) - max(lo, h)) / length
                        for h in range(-n_history, 0) if min(hi, h + 1) > max(lo, h)}
        out.append((from_blocks, from_history))
    return out


def optimize(cascade: Cascade, price: np.ndarray, inflow: np.ndarray, state: State, terminal: Terminal,
             step_h: float = 0.25, blocks: list[int] | None = None, commitment: np.ndarray | None = None,
             reserve_price: np.ndarray | None = None, reserve_cap: np.ndarray | None = None,
             reserve_fixed: np.ndarray | None = None, mip_gap: float = 0.002, time_limit: float = 120,
             steady: np.ndarray | None = None) -> Plan:
    """Optimal körplan givet priser (€/MWh) och lokal tillrinning (perioder, kraftverk) i m³/s.
    commitment är budåtagandet i MW för de första perioderna (vanligen ett dygn).
    reserve_price (perioder, 5) är stödtjänstpriser i €/MW per timme, reserve_cap (5,) högsta
    volym i MW per produkt och reserve_fixed (perioder, 5) redan sålda volymer i början (NaN för
    produkter som ännu inte är sålda). steady (perioder, kraftverk) är dygnsnumret där
    korttidsreglering är förbjuden, annars -1 (se Cascade.steady_days)."""
    prob = pulp.LpProblem("kaskad", pulp.LpMaximize)
    objective, _, _, extract = _add_scenario(prob, cascade, price, inflow, state, terminal, step_h, blocks,
                                             commitment, reserve_price, reserve_cap, reserve_fixed, steady=steady)
    prob += objective
    return extract(_solve(prob, mip_gap, time_limit))


def optimize_scenarios(cascade: Cascade, prices: np.ndarray, weights: np.ndarray, inflow: np.ndarray,
                       state: State, terminal: Terminal, step_h: float = 0.25,
                       reserve_price: np.ndarray | None = None, reserve_cap: np.ndarray | None = None,
                       shared: tuple[int, ...] = (), shared_steps: int = 0, bid_steps: int = 0,
                       mip_gap: float = 0.005, time_limit: float = 300,
                       steady: np.ndarray | None = None) -> list[Plan]:
    """Tvåstegsmodell över prisscenarier (scenarier, perioder) med sannolikheterna weights. Varje
    scenario har sin egen körplan, men två saker bestäms innan priset är känt:
    - stödtjänsterna i shared (index som i data.RESERVES) är gemensamma under de första
      shared_steps perioderna,
    - produktionen under de första bid_steps perioderna ska gå att lägga som en budkurva: i
      varje period är den aldrig lägre i ett scenario med högre pris.
    Målet är det förväntade värdet, så till exempel reservintäkten vägs mot vad den kostar i
    förlorad flexibilitet i varje scenario."""
    prob = pulp.LpProblem("kaskad_scenarier", pulp.LpMaximize)
    parts = [_add_scenario(prob, cascade, p, inflow, state, terminal, step_h, reserve_price=reserve_price,
                           reserve_cap=reserve_cap, tag=f"_s{s}", steady=steady) for s, p in enumerate(prices)]
    edges = np.concatenate([[0], np.cumsum(time_blocks(prices.shape[1], step_h))])
    common = {}
    for b in range(len(edges) - 1):
        for i in shared if edges[b] < shared_steps else ():
            key = (int(edges[b] * step_h), i)
            if key not in common:
                common[key] = prob.add_variable(f"gemensam_{key[0]}_{i}", lowBound=0)
            for _, _, total_reserve, _ in parts:
                prob += total_reserve[b, i] == common[key]
        if edges[b] < bid_steps:
            block_price = prices[:, edges[b]:edges[b + 1]].mean(axis=1)
            order = np.argsort(block_price, kind="stable")
            for low, high in zip(order[:-1], order[1:]):
                lower, higher = parts[low][1][b], parts[high][1][b]
                if block_price[high] - block_price[low] < 1e-6:
                    prob += lower == higher
                else:
                    prob += lower <= higher
    prob += pulp.lpSum(float(w) * objective for w, (objective, _, _, _) in zip(weights, parts))
    stats = _solve(prob, mip_gap, time_limit)
    return [extract(stats) for _, _, _, extract in parts]


def _solve(prob: pulp.LpProblem, mip_gap: float, time_limit: float):
    # En tråd per lösning: backtesten kör flera strategier parallellt i egna processer. Hittas
    # ingen tillåten lösning inom tidsgränsen, till exempel för att datorn har sovit under
    # lösningen (gränsen räknas i verklig tid), görs ett nytt försök med tio gånger längre tid.
    # HiGHS kan också avbryta med ett numeriskt fel i en LP-lösning ("Solve error"). Det beror på
    # vägen genom sökningen och inte på problemet, så då görs nya försök med andra slumpfrön.
    limit, seeds, options = time_limit, list(RETRY_SEEDS), {}
    while True:
        stats = prob.solve(pulp.HiGHS(msg=False, gapRel=mip_gap, timeLimit=limit, threads=1, **options))
        if stats.has_solution:
            return stats
        if stats.status == pulp.LpSolveStatus.TimeLimit and limit == time_limit:
            limit = 10 * time_limit
        elif stats.status != pulp.LpSolveStatus.TimeLimit and seeds:
            options = {"random_seed": seeds.pop(0)}
        else:
            raise RuntimeError(f"Optimeringen hittade ingen lösning (status: {stats.status.name})")


def _add_scenario(prob: pulp.LpProblem, cascade: Cascade, price: np.ndarray, inflow: np.ndarray, state: State,
                  terminal: Terminal, step_h: float = 0.25, blocks: list[int] | None = None,
                  commitment: np.ndarray | None = None, reserve_price: np.ndarray | None = None,
                  reserve_cap: np.ndarray | None = None, reserve_fixed: np.ndarray | None = None, tag: str = "",
                  steady: np.ndarray | None = None):
    """Lägger in ett prisscenarios variabler och villkor i prob (tag skiljer variabelnamnen åt).
    Returnerar (målfunktion, kaskadens effekt per block, kaskadens reserv per block och produkt,
    funktion som läser ut planen när problemet är löst)."""
    plants = cascade.plants
    blocks = time_blocks(len(price), step_h) if blocks is None else blocks
    edges = np.concatenate([[0], np.cumsum(blocks)])
    B, P, T = range(len(blocks)), range(len(plants)), int(edges[-1])
    dur = np.array(blocks, dtype=float) * step_h
    price_b = np.array([price[edges[b]:edges[b + 1]].mean() for b in B])
    inflow_b = np.array([inflow[edges[b]:edges[b + 1]].mean(axis=0) for b in B])
    head = [float(plant.head_factor(np.clip(state.volume[p], 0, plant.v_max))) for p, plant in enumerate(plants)]
    c = MM3_PER_M3S_HOUR

    def variable(name: str, **bounds):
        return prob.add_variable(name + tag, **bounds)

    z, start, seg = {}, {}, {}
    for p, plant in enumerate(plants):
        for u in range(plant.n_units):
            for b in B:
                z[p, u, b] = variable(f"z_{p}_{u}_{b}", cat="Binary")
                start[p, u, b] = variable(f"start_{p}_{u}_{b}", lowBound=0)
                for k, (width, _) in enumerate(plant.unit.segments()):
                    seg[p, u, b, k] = variable(f"q_{p}_{u}_{b}_{k}", lowBound=0, upBound=width)
    spill = {(p, b): variable(f"spill_{p}_{b}", lowBound=0) for p in P for b in B}
    vol = {(p, b): variable(f"v_{p}_{b}", lowBound=0, upBound=plants[p].v_max) for p in P for b in B}
    shortfall = {(p, b): variable(f"under_min_{p}_{b}", lowBound=0)
                 for p in P if plants[p].min_flow for b in B}
    below_margin = {(p, b): variable(f"under_marginal_{p}_{b}", lowBound=0)
                    for p in P if plants[p].min_flow for b in B}
    # Slutvolymen uppdelad på vattenvärdesfunktionens segment (fylls nedifrån eftersom den är konkav).
    wv_seg = {(p, j): variable(f"vv_{p}_{j}", lowBound=0, upBound=float(width))
              for p in P for j, width in enumerate(np.diff(terminal.edges[p]))}

    def unit_flow(p, u, b):
        unit = plants[p].unit
        return unit.q_min * z[p, u, b] + pulp.lpSum(seg[p, u, b, k] for k in range(len(unit.segments())))

    def unit_power(p, u, b):
        unit = plants[p].unit
        return head[p] * (unit.p_min * z[p, u, b] + pulp.lpSum(
            slope * seg[p, u, b, k] for k, (_, slope) in enumerate(unit.segments())))

    flow = {(p, b): pulp.lpSum(unit_flow(p, u, b) for u in range(plants[p].n_units)) for p in P for b in B}
    release = {(p, b): flow[p, b] + spill[p, b] for p in P for b in B}
    total_power = {b: pulp.lpSum(unit_power(p, u, b) for p in P for u in range(plants[p].n_units)) for b in B}

    arrival = {}
    for p in range(1, len(plants)):
        delay = round(plants[p - 1].delay_to_next / step_h)
        weights = _overlaps(edges, delay, len(state.release_history))
        for b, (from_blocks, from_history) in zip(B, weights):
            arrival[p, b] = (pulp.lpSum(w * release[p - 1, j] for j, w in from_blocks.items())
                             + sum(w * float(state.release_history[h, p - 1]) for h, w in from_history.items()))

    revenue = pulp.lpSum(dur[b] * price_b[b] * total_power[b] for b in B)
    start_costs = pulp.lpSum(plants[p].unit.start_cost * start[p, u, b]
                             for p in P for u in range(plants[p].n_units) for b in B)
    # Förändringen i vattnets värde, så att målfunktionen (och därmed MIP-gapet) får rätt skala.
    water_value = (pulp.lpSum(float(terminal.slopes[p][j]) * var for (p, j), var in wv_seg.items())
                   - terminal.value(np.clip(state.volume, 0, [pl.v_max for pl in plants])))
    penalties = (pulp.lpSum(SPILL_PENALTY * (T - edges[b]) * step_h * dur[b] * spill[p, b] for p in P for b in B)
                 + pulp.lpSum(MIN_FLOW_PENALTY * dur[b] * var for (p, b), var in shortfall.items())
                 + pulp.lpSum(SAFETY_PENALTY * dur[b] * var for (p, b), var in below_margin.items()))
    for p, plant in enumerate(plants):
        if not plant.ramp and not plant.steady_months:
            continue
        label = (np.full(T, -1) if steady is None else np.asarray(steady)[:T, p])
        block_label = label[edges[:-1]]
        for b in B:
            if b == 0:
                previous, gap_h, same_day = float(state.release_history[-1, p]), (step_h + dur[0]) / 2, False
            else:
                previous, gap_h = release[p, b - 1], (dur[b - 1] + dur[b]) / 2
                same_day = block_label[b] >= 0 and block_label[b] == block_label[b - 1]
            over = variable(f"over_ramp_{p}_{b}", lowBound=0)
            # Utan korttidsreglering tappas dygnets medelnivå (simulation.regulate), så ett brott, mot
            # dygnet före eller inom dygnet, flyttar hela dygnets tappning och kostar för hela dygnet.
            # Annars vore det billigt att klara gränsen första kvarten och sedan ändra inom dygnet.
            weight = dur[block_label == block_label[b]].sum() if block_label[b] >= 0 else dur[b]
            if same_day:
                prob += release[p, b] - previous <= over
                prob += previous - release[p, b] <= over
            else:
                limit = plant.steady_change if block_label[b] >= 0 else (plant.ramp * gap_h if plant.ramp else None)
                if limit is None:
                    continue
                prob += release[p, b] - previous <= limit + over
                prob += previous - release[p, b] <= limit + over
            penalties += RAMP_PENALTY * weight * over
    if commitment is not None:
        # Buden är redan lagda: avvikelser mot åtagandet kostar (avräknas mot obalanspriset).
        for b in range(len(commitment)):
            over = variable(f"over_bud_{b}", lowBound=0)
            under = variable(f"under_bud_{b}", lowBound=0)
            prob += total_power[b] - float(commitment[b]) == over - under
            penalties += IMBALANCE_PENALTY * dur[b] * (over + under)

    reserve, total_reserve = {}, {}
    if reserve_price is not None:
        reserve_b = np.array([reserve_price[edges[b]:edges[b + 1]].mean(axis=0) for b in B])
        products = range(reserve_b.shape[1])
        reserve = {(p, b, i): variable(f"reserv_{p}_{b}_{i}", lowBound=0) for p in P for b in B for i in products}
        for p, plant in enumerate(plants):
            unit = plant.unit
            for b in B:
                n_on = pulp.lpSum(z[p, u, b] for u in range(plant.n_units))
                cap_on = head[p] * unit.p_max * n_on
                power_p = pulp.lpSum(unit_power(p, u, b) for u in range(plant.n_units))
                fcr_n, fcr_up, fcr_down, mfrr_up, mfrr_down = (reserve[p, b, i] for i in products)
                prob += power_p + fcr_n + fcr_up <= cap_on
                prob += power_p + fcr_n + fcr_up + mfrr_up <= head[p] * unit.p_max * plant.n_units
                prob += power_p - fcr_n - fcr_down >= head[p] * unit.p_min * n_on
                prob += power_p - fcr_n - fcr_down - mfrr_down >= 0
                prob += fcr_n <= FCR_N_SHARE * cap_on
                prob += fcr_up <= FCR_D_SHARE * cap_on
                prob += fcr_down <= FCR_D_SHARE * cap_on
        total_reserve = {(b, i): pulp.lpSum(reserve[p, b, i] for p in P) for b in B for i in products}
        hour_start = {}
        for b in B:
            hour = int(edges[b] * step_h)
            if dur[b] < 1 and hour in hour_start:
                for i in products:   # samma volym hela timmen, som på marknaden
                    prob += total_reserve[b, i] == total_reserve[hour_start[hour], i]
            hour_start.setdefault(hour, b)
            for i in products:
                if reserve_cap is not None:
                    prob += total_reserve[b, i] <= float(reserve_cap[i])
                sold = np.nan if reserve_fixed is None or edges[b] >= len(reserve_fixed) else reserve_fixed[edges[b], i]
                if not np.isnan(sold):
                    # Redan såld volym ska levereras; går det inte kostar underleveransen.
                    missing = variable(f"reserv_saknas_{b}_{i}", lowBound=0)
                    prob += total_reserve[b, i] + missing == float(sold)
                    penalties += RESERVE_SHORTFALL_PENALTY * dur[b] * missing
        revenue += pulp.lpSum(dur[b] * reserve_b[b, i] * total_reserve[b, i] for b in B for i in products)
    objective = revenue - start_costs + water_value - penalties

    for p, plant in enumerate(plants):
        for u in range(plant.n_units):
            was_on = 1 if u < state.units_on[p] else 0
            for b in B:
                for k, (width, _) in enumerate(plant.unit.segments()):
                    prob += seg[p, u, b, k] <= width * z[p, u, b]
                prob += start[p, u, b] >= z[p, u, b] - (z[p, u, b - 1] if b > 0 else was_on)
                if u > 0:  # identiska aggregat: aggregat 1 startas före aggregat 2 (bryter symmetri)
                    prob += z[p, u, b] <= z[p, u - 1, b]
        v_start = float(np.clip(state.volume[p], 0, plant.v_max))
        for b in B:
            v_prev = vol[p, b - 1] if b > 0 else v_start
            prob += vol[p, b] == v_prev + c * dur[b] * (inflow_b[b, p] + arrival.get((p, b), 0) - release[p, b])
            if plant.min_flow:
                prob += release[p, b] + shortfall[p, b] >= plant.min_flow
                prob += vol[p, b] + below_margin[p, b] >= SAFETY_MARGIN * plant.v_max
        prob += pulp.lpSum(wv_seg[p, j] for j in range(len(terminal.slopes[p]))) == vol[p, len(blocks) - 1]

    def extract(stats) -> Plan:
        shape = (len(blocks), len(plants))
        units = np.zeros(shape, dtype=int)
        power = np.zeros(shape)
        for (p, u, b), z_var in z.items():
            units[b, p] += round(z_var.value())
            power[b, p] += unit_power(p, u, b).value()
        expand = lambda a: np.repeat(a, blocks, axis=0)  # noqa: E731
        best = stats.best_bound if stats.best_bound is not None else stats.objective
        reserves = None
        if reserve:
            n_products = reserve_price.shape[1]
            reserves = expand(np.array([[sum(reserve[p, b, i].value() for p in P) for i in range(n_products)]
                                        for b in B]))
        return Plan(
            flow=expand(np.array([[flow[p, b].value() for p in P] for b in B])),
            spill=expand(np.array([[spill[p, b].value() for p in P] for b in B])),
            units=expand(units),
            power=expand(power),
            volume=expand(np.array([[vol[p, b].value() for p in P] for b in B])),
            objective=objective.value(),
            gap=abs(best - stats.objective) / max(abs(stats.objective), 1.0),
            reserves=reserves,
        )

    return objective, total_power, total_reserve, extract
