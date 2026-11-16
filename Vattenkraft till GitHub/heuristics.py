"""Tumregler för kaskaden, planerade ett dygn i taget.

Varje kraftverk räknar ut hur mycket vatten det ska släppa under dygnet: det som
väntas komma in (lokal tillrinning plus planerad tappning från kraftverket ovanför,
förskjuten med gångtiden) plus det som ska tas ur magasinet för att följa
styrkurvan. Varje period släpps minst det som krävs för minimitappningen längre ned
i älven. Är det mindre än minsta last körs i stället ett aggregat på minsta last en del av
dygnet, och dammen nedströms jämnar ut flödet. Resten av vattnet fördelas så här:

- Jämn tappning: lika mycket varje period. Räcker vattnet inte till minsta last
  hela dygnet körs ett aggregat på minsta last så länge det räcker.
- Dygnstopp: på fullt under dygnets dyraste perioder. Det är den vanliga tumregeln: kör när
  det är dyrt. I en variant körs ett kraftverk aldrig mer än kraftverket nedströms kan ta
  emot, i den andra körs det fullt och överskottet spills nedströms.
- Veckotopp: som dygnstopp, men det översta magasinets vatten för veckan läggs på veckans
  dyraste perioder enligt prisprognosen, så att dyra dygn får mer vatten än billiga.

Vattenvärdesbud följer inte styrkurvan: det översta kraftverket bjuder in vatten när priset är
minst vattenvärdet vid aktuell fyllnadsgrad (från samma SDP som optimeringen). Budet har två
steg: upp till vad kraftverket nedströms tar emot vid vattenvärdet, och fullt först vid ett
högre pris, eftersom vattnet därutöver spills vid nästa kraftverk. Det är den vanliga enkla
metoden i nordisk vattenkraft.
"""
from __future__ import annotations

import numpy as np

from cascade import MM3_PER_M3S_HOUR, Cascade
from model import State

# Reglerna följer inte magasinen kvart för kvart och kan tömma Åsens lilla magasin. Då klaras
# minimitappningen bara om Trängslet släpper tillräckligt, och den lokala tillrinningen
# däremellan blir ibland lägre än prognosen (högst 17 % under för första dygnet i backtesten).
INFLOW_MARGIN = 0.2
# Körs ett kraftverk i block på minsta last ska dammen nedströms enligt prognosen aldrig gå under
# den här andelen. Annars släpps det lilla flödet förbi turbinen.
MIN_LOAD_BUFFER = 0.05


def _required_release(cascade: Cascade, inflow: np.ndarray) -> np.ndarray:
    """Lägsta tappning per period och kraftverk för att minimitappningen ska klaras
    här och nedströms, med hjälp av den lokala tillrinningen däremellan. Tillrinningen är en
    prognos, så den räknas med en säkerhetsmarginal (INFLOW_MARGIN)."""
    H, n = inflow.shape
    floor = np.zeros((H, n))
    for p in range(n):
        local = np.zeros(H)
        for d in range(p, n):
            if d > p:
                local += (1 - INFLOW_MARGIN) * inflow[:, d]
            floor[:, p] = np.maximum(floor[:, p], cascade.plants[d].min_flow - local)
    return floor


def _fill(order, base: np.ndarray, extra: float, level: float) -> np.ndarray:
    """Lägger extra vatten period för period i given ordning, upp till level m³/s."""
    flow = base.copy()
    for t in order:
        add = min(level - flow[t], extra)
        if add > 0:
            flow[t] += add
            extra -= add
        if extra <= 1e-9:
            break
    return flow


def _min_load_block(flow: np.ndarray, q_min: float, price: np.ndarray, tail: int = 0) -> np.ndarray:
    """Flöden under minsta last går förbi turbinen och ger ingen el. I stället körs ett aggregat på
    minsta last under de dyraste av de perioderna, med minst lika mycket vatten, och dammen
    nedströms jämnar ut flödet. De sista tail perioderna når nästa kraftverk först nästa dygn,
    som planeras utan att kunna ändra dem, så de körs alltid."""
    low = np.flatnonzero((flow > 1e-9) & (flow < q_min - 1e-9))
    if len(low) == 0:
        return flow
    n_on = int(np.ceil(flow[low].sum() / q_min - 1e-9))
    last = low[low >= len(flow) - tail]
    rest = low[low < len(flow) - tail]
    out = flow.copy()
    out[low] = 0.0
    out[last] = q_min
    out[rest[np.argsort(-price[rest], kind="stable")[:max(n_on - len(last), 0)]]] = q_min
    return out


def buffer_holds(cascade: Cascade, expected) -> bool:
    """Om dammarna med minimitappning klarar att jämna ut flödet enligt prognosen (expected är
    planens utfall mot prognosen): ingen brist och aldrig under MIN_LOAD_BUFFER av dammen."""
    for p, plant in enumerate(cascade.plants):
        if plant.min_flow and (expected.shortfall[:, p].max() > 0
                               or expected.volume[:, p].min() < MIN_LOAD_BUFFER * plant.v_max):
            return False
    return True


def _plan_top_down(cascade: Cascade, shape, state: State, inflow: np.ndarray, targets: np.ndarray, step_h: float,
                   price: np.ndarray, fixed: dict[int, np.ndarray] | None = None,
                   min_load_blocks: bool = True) -> np.ndarray:
    """Planerar kraftverken uppifrån och ned. shape(p, base, extra) ger periodernas flöden, utom för
    kraftverk vars flöden redan är bestämda i fixed. Vattenmängder räknas i (m³/s)·perioder.
    Med min_load_blocks körs små flöden som block på minsta last där det finns en damm nedströms."""
    H = len(inflow)
    floor = _required_release(cascade, inflow)
    plan = np.zeros((H, len(cascade.plants)))
    for p, plant in enumerate(cascade.plants):
        expected = inflow[:, p].copy()
        if p > 0:
            delay = round(cascade.plants[p - 1].delay_to_next / step_h)
            history = state.release_history[len(state.release_history) - delay:, p - 1]
            expected += np.concatenate([history, plan[:, p - 1]])[:H]
        budget = expected.sum() + (state.volume[p] - targets[p]) / (MM3_PER_M3S_HOUR * step_h)
        # Hellre ett aggregat på minsta last än att släppa vattnet förbi turbinen, om vattnet räcker.
        raised = np.where((floor[:, p] > 0) & (floor[:, p] < plant.unit.q_min), plant.unit.q_min, floor[:, p])
        if raised.sum() <= budget:
            floor[:, p] = raised
        if fixed and p in fixed:
            plan[:, p] = np.maximum(fixed[p], floor[:, p])
        else:
            budget = np.clip(budget, floor[:, p].sum(), H * plant.q_max)
            plan[:, p] = shape(p, floor[:, p], budget - floor[:, p].sum())
        if min_load_blocks and p + 1 < len(cascade.plants):
            plan[:, p] = _min_load_block(plan[:, p], plant.unit.q_min, price, round(plant.delay_to_next / step_h))
    return plan


def even_release(cascade: Cascade, price: np.ndarray, state: State, inflow: np.ndarray,
                 targets: np.ndarray, step_h: float = 0.25, min_load_blocks: bool = True) -> np.ndarray:
    def shape(p: int, base: np.ndarray, extra: float) -> np.ndarray:
        level = max((base.sum() + extra) / len(base), cascade.plants[p].unit.q_min)
        return _fill(range(len(base)), base, extra, level)

    return _plan_top_down(cascade, shape, state, inflow, targets, step_h, price, min_load_blocks=min_load_blocks)


def daily_peak(cascade: Cascade, price: np.ndarray, state: State, inflow: np.ndarray,
               targets: np.ndarray, step_h: float = 0.25, cap_downstream: bool = True,
               min_load_blocks: bool = True) -> np.ndarray:
    plants = cascade.plants

    def shape(p: int, base: np.ndarray, extra: float) -> np.ndarray:
        level = plants[p].q_max
        if cap_downstream and p + 1 < len(plants):
            level = min(level, plants[p + 1].q_max)
        return _fill(np.argsort(-price, kind="stable"), base, extra, level)

    return _plan_top_down(cascade, shape, state, inflow, targets, step_h, price, min_load_blocks=min_load_blocks)


def water_value_bid(cascade: Cascade, price: np.ndarray, state: State, inflow: np.ndarray,
                    targets: np.ndarray, water_value: float, step_h: float = 0.25,
                    min_load_blocks: bool = True) -> np.ndarray:
    """Översta kraftverket bjuder i två steg. water_value är vattnets värde i €/Mm³. Kraftverken
    nedströms kör det vatten som kommer under dygnets dyraste perioder."""
    plants = cascade.plants
    energy = cascade.downstream_energy()                      # MWh per Mm³ genom alla kraftverk nedströms
    first = water_value / energy[0]                           # €/MWh när allt vatten går genom turbinerna
    second = water_value / (energy[0] - plants[1].energy_equivalent)  # när överskottet spills vid nästa
    upper = np.where(price >= second, plants[0].q_max,
                     np.where(price >= first, min(plants[0].q_max, plants[1].q_max), 0.0))

    def shape(p: int, base: np.ndarray, extra: float) -> np.ndarray:
        return _fill(np.argsort(-price, kind="stable"), base, extra, plants[p].q_max)

    return _plan_top_down(cascade, shape, state, inflow, targets, step_h, price, fixed={0: upper},
                          min_load_blocks=min_load_blocks)


def weekly_peak(cascade: Cascade, price: np.ndarray, state: State, inflow: np.ndarray,
                targets: np.ndarray, step_h: float = 0.25, min_load_blocks: bool = True) -> np.ndarray:
    """price och inflow gäller en vecka och targets vid veckans slut. Returnerar planen för första dygnet."""
    per_day = len(price) // 7
    upper = cascade.plants[0]
    to_volume = MM3_PER_M3S_HOUR * step_h
    week_water = inflow[:, 0].sum() + (state.volume[0] - targets[0]) / to_volume
    week_plan = _fill(np.argsort(-price, kind="stable"), np.zeros(len(price)),
                      float(np.clip(week_water, 0, len(price) * upper.q_max)), upper.q_max)
    # Det som hamnar på första dygnet blir dygnets mål för magasinet; dammarna hålls halvfulla.
    day_targets = targets.copy()
    day_targets[0] = state.volume[0] - (week_plan[:per_day].sum() - inflow[:per_day, 0].sum()) * to_volume
    return daily_peak(cascade, price[:per_day], state, inflow[:per_day], day_targets, step_h, cap_downstream=False,
                      min_load_blocks=min_load_blocks)
