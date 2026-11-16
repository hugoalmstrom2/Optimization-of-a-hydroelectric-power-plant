"""Backtest: strategierna körs dygn för dygn genom en period med verkliga priser och tillrinning.

Varje dygn D går till som på day-ahead-marknaden:

1. Bud (dagen innan, när D:s priser inte är kända). MILP-strategin optimerar för fem
   prisscenarier (prognos plus verkliga prognosfel från de senaste veckorna) i en
   tvåstegsmodell med samma horisont som leveransplaneringen. Produktionen första dygnet får
   aldrig vara lägre i ett scenario med högre pris, så scenarierna bildar direkt en budkurva
   per kvart: hur mycket kaskaden producerar vid varje pris. Under det lägsta scenariots pris
   sjunker budet till noll vid vattenvärdet.
   Tumreglerna lägger fasta bud efter sin plan med prisprognosen.
2. Clearing: det verkliga spotpriset avgör åtagandet per kvart.
3. Leverans: planen optimeras om mot åtagandet, nu med D:s priser kända, och genomförs
   mot den faktiska tillrinningen.
4. Avräkning: åtagandet betalas med spotpriset och avvikelser med det verkliga
   obalanspriset (eSett).

Stödtjänster (FCR-N, FCR-D upp/ned, mFRR upp/ned) säljs dagen innan som fasta volymer per
timme. mFRR-kapacitet auktioneras före day-ahead: i MILP-strategin är volymen gemensam för
alla prisscenarier i tvåstegsmodellen, som väger reservintäkten (prognostiserat pris) mot
förlorad flexibilitet. FCR auktioneras efter day-ahead och bestäms med leveransplanen, när
åtagandet är känt.
Tumreglerna säljer den kapacitet som blir ledig enligt deras plan. Volymerna betalas med det
verkliga kapacitetspriset, men bara den del som faktiskt gick att leverera med utfallet.
Kaskaden får sälja högst en fjärdedel av vad elområdet upphandlar.

Tillståndet (magasin, vatten på väg mellan stationerna, aggregat i drift) förs vidare.
Vattenvärdena räknas med SDP (watervalues.py) och räknas om vid varje månadsskifte med
den data som fanns då, så varken optimeringen eller värderingen av vattnet som är kvar vid
periodens slut ser framtida priser. Läget sparas efter varje vecka i data_cache/backtest/,
så att en avbruten körning fortsätter där den slutade. Sparpunkterna gäller bara så länge
koden är oförändrad.
"""
from __future__ import annotations

import hashlib
import os
import pickle
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from cascade import Cascade
from data import RESERVES
from forecast import inflow_path, price_path, price_scenarios, scenario_weights
from heuristics import buffer_holds, daily_peak, even_release, water_value_bid, weekly_peak
from model import State, Terminal, optimize, optimize_scenarios
from simulation import Outcome, deliverable_reserves, execute, reserve_room
from watervalues import MonthlyWaterValues, WaterValues

STEP_H = 0.25                           # kvartar, som day-ahead-marknaden
STEPS_PER_DAY = 96
HORIZON_DAYS = 7
BID_SCENARIOS = 5                       # prisscenarier per budkurva, från minsta till största prognosfelet
POND_BAND = 0.1                         # dammarnas vattenvärde är flackt ±10 % runt halvfull
POND_SLOPES = (2.0, 1.05, 0.95, 0.3)    # andel av vattenvärdet: långt under, nära under, nära över, långt över
GUIDE_BAND = 0.01                       # "följer styrkurvan": fritt ±1 % av magasinet runt styrkurvan
GUIDE_SLOPES = (3.0, 0.2)               # utanför bandet: marginalvärde minst 3x under, högst 0,2x över
RESERVE_SHARE = 0.25                    # högst en fjärdedel av elområdets upphandlade volym (antagande)
RESERVE_VOLUME_DAYS = 28                # volymtaket sätts efter medianen de senaste fyra veckorna
FCR = [RESERVES.index(r) for r in ("fcr_n", "fcr_d_upp", "fcr_d_ned")]   # auktioneras efter day-ahead
MFRR = tuple(RESERVES.index(r) for r in ("mfrr_upp", "mfrr_ned"))       # auktioneras före day-ahead
DAY = pd.Timedelta(days=1)
CHECKPOINTS = Path(__file__).parent / "data_cache" / "backtest"


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    kind: str                           # "regel", "milp" eller "perfekt"
    rule: str | None = None             # tumregel: "jamn" eller "dygnstopp"
    cap_downstream: bool = True         # tumregel: kör aldrig mer än stationen nedströms tar emot
    follow_guide: bool = False          # håll övre magasinet nära styrkurvan (ren korttidsvinst)
    water_value_scale: float = 1.0      # känslighetsanalys
    start_cost_scale: float = 1.0       # känslighetsanalys
    reserve_scale: float = 1.0          # känslighetsanalys: andel av stödtjänsttaket, 0 = inga stödtjänster
    risk_averse: bool = False           # känslighetsanalys: riskaverta vattenvärden (CVaR)
    scenarios: int = BID_SCENARIOS      # prisscenarier i budgivningen; 1 = bara prisprognosen


STRATEGIES = (
    Strategy("jamn", "Jämn tappning", "regel", rule="jamn"),
    Strategy("dygnstopp", "Dygnstopp, med tak", "regel", rule="dygnstopp"),
    Strategy("dygnstopp_fullt", "Dygnstopp, Trängslet fullt", "regel", rule="dygnstopp", cap_downstream=False),
    Strategy("veckotopp", "Veckotopp", "regel", rule="veckotopp"),
    Strategy("vattenvarde", "Vattenvärdesbud", "regel", rule="vattenvarde"),
    Strategy("milp", "MILP med budgivning", "milp"),
    # Samma optimering med bara prisprognosen, som en deterministisk planering: visar vad
    # scenarierna och budkurvan tillför.
    Strategy("deterministisk", "MILP med punktprognos", "milp", scenarios=1),
    Strategy("styrkurva", "MILP som följer styrkurvan", "milp", follow_guide=True),
    Strategy("perfekt", "MILP, perfekt information", "perfekt"),
)
SENSITIVITY = (
    Strategy("vv_ned", "Vattenvärde −20 %", "milp", water_value_scale=0.8),
    Strategy("vv_upp", "Vattenvärde +20 %", "milp", water_value_scale=1.2),
    Strategy("start_ned", "Startkostnad −50 %", "milp", start_cost_scale=0.5),
    Strategy("start_upp", "Startkostnad +100 %", "milp", start_cost_scale=2.0),
    Strategy("vv_risk", "Riskaverta vattenvärden", "milp", risk_averse=True),
    Strategy("res_halv", "Halverat stödtjänsttak", "milp", reserve_scale=0.5),
    Strategy("res_utan", "Utan stödtjänster", "milp", reserve_scale=0.0),
    Strategy("styrkurva_res_utan", "Följer styrkurvan, utan stödtjänster", "milp", follow_guide=True,
             reserve_scale=0.0),
    # Vattenvärdesbudet använder också vattenvärdena och får samma antagande som MILP.
    Strategy("vattenvarde_vv_ned", "Vattenvärdesbud, vattenvärde −20 %", "regel", rule="vattenvarde",
             water_value_scale=0.8),
    Strategy("vattenvarde_vv_upp", "Vattenvärdesbud, vattenvärde +20 %", "regel", rule="vattenvarde",
             water_value_scale=1.2),
    Strategy("vattenvarde_risk", "Vattenvärdesbud, riskaverta vattenvärden", "regel", rule="vattenvarde",
             risk_averse=True),
) + tuple(replace(s, key=f"{s.key}_res_{name}", label=f"{s.label}, {label}", reserve_scale=scale)
          for scale, name, label in ((0.5, "halv", "halverat stödtjänsttak"), (0.0, "utan", "utan stödtjänster"))
          for s in STRATEGIES if s.kind == "regel")
RULES = {"jamn": even_release, "dygnstopp": daily_peak, "veckotopp": weekly_peak}


@dataclass
class Result:
    outcome: Outcome            # perioder för hela backtesten
    commitment: np.ndarray      # budåtagande per period (MW, hela kaskaden)
    reserves: np.ndarray        # sålda stödtjänster per period och produkt (MW, hela kaskaden)
    final_state: State
    mip_gaps: list[float]


def reserve_market(reserves: pd.DataFrame, day: pd.Timestamp, steps: int, known: bool) -> tuple[np.ndarray, np.ndarray]:
    """Stödtjänstpriser per kvart för horisonten (verkliga om known, annars prognos gjord dagen
    innan) och kaskadens volymtak per produkt (MW)."""
    hours = steps // 4
    prices = []
    for product in RESERVES:
        series = reserves[f"pris_{product}"]
        if known:
            prices.append(series[day:day + pd.Timedelta(hours=hours - 1)].to_numpy())
        else:
            prices.append(price_path(series[series.index < day], day, hours)[0])
    recent = reserves[(reserves.index < day) & (reserves.index >= day - pd.Timedelta(days=RESERVE_VOLUME_DAYS))]
    cap = RESERVE_SHARE * np.array([recent[f"volym_{p}"].median() for p in RESERVES])
    return np.repeat(np.column_stack(prices), 4, axis=0), np.nan_to_num(cap)


def hourly_offer(room: np.ndarray, cap: np.ndarray) -> np.ndarray:
    """Tumreglernas försäljning: så mycket som ryms i marginalerna under timmens alla kvartar,
    högst taket. FCR-N säljs först, sedan FCR-D och sist mFRR."""
    available = deliverable_reserves(np.tile(cap, (len(room), 1)), room)
    per_hour = available.reshape(-1, 4, available.shape[1]).min(axis=1)
    return np.repeat(per_hour, 4, axis=0)


def with_start_costs(cascade: Cascade, factor: float) -> Cascade:
    if factor == 1.0:
        return cascade
    return replace(cascade, plants=tuple(replace(p, unit=replace(p.unit, start_cost=p.unit.start_cost * factor))
                                         for p in cascade.plants))


def targets(cascade: Cascade, when: pd.Timestamp, wv: WaterValues) -> np.ndarray:
    """Målvolymer: styrkurvan för magasinet, halvfullt för dammarna."""
    return np.array([wv.target(when)] + [0.5 * p.v_max for p in cascade.plants[1:]])


def terminal(cascade: Cascade, when: pd.Timestamp, wv: WaterValues, follow_guide: bool = False,
             scale: float = 1.0) -> Terminal:
    """Vattenvärdesfunktioner vid tidpunkten when. Magasinet får SDP:ns funktion, eventuellt
    förankrad vid styrkurvan. Dammarna får en flack kurva runt halvfull, med samma prisnivå
    gånger energin nedströms."""
    energy = cascade.downstream_energy()
    level = wv.level(when, energy[0])
    slopes = wv.marginal(when)
    if follow_guide:
        # Brant under och flackt över ett smalt band runt styrkurvan; konkaviteten bevaras.
        middle = (wv.volumes[:-1] + wv.volumes[1:]) / 2
        band = GUIDE_BAND * cascade.plants[0].v_max
        steep, flat = GUIDE_SLOPES[0] * level * energy[0], GUIDE_SLOPES[1] * level * energy[0]
        slopes = np.where(middle < wv.target(when) - band, np.maximum(slopes, steep), slopes)
        slopes = np.where(middle > wv.target(when) + band, np.minimum(slopes, flat), slopes)
    edges, all_slopes = [wv.volumes], [slopes]
    for plant, e in zip(cascade.plants[1:], energy[1:]):
        mid, band = 0.5 * plant.v_max, POND_BAND * plant.v_max
        edges.append(np.array([0, mid - band, mid, mid + band, plant.v_max]))
        all_slopes.append(level * e * np.array(POND_SLOPES))
    return Terminal(edges, all_slopes).scaled(scale)


def initial_state(cascade: Cascade, start: pd.Timestamp, inflows: pd.DataFrame, wv: WaterValues) -> State:
    """Magasinen på styrkurvan, inga aggregat i drift och naturligt flöde på väg nedströms."""
    natural = np.cumsum(inflows.loc[start].to_numpy())
    return State(volume=targets(cascade, start, wv),
                 release_history=np.tile(natural, (round(cascade.max_delay / STEP_H), 1)),
                 units_on=np.zeros(len(cascade.plants), dtype=int))


def clear_bids(scenario_prices: np.ndarray, scenario_power: np.ndarray, price: np.ndarray,
               water_value: float) -> np.ndarray:
    """Budkurva per period ur scenarierna (pris -> effekt, aldrig minskande med priset) och
    åtagandet vid det verkliga priset. Under det lägsta scenariots pris sjunker budet linjärt
    till noll vid vattenvärdet (€/MWh): vatten säljs inte billigare än det är värt. Med ett
    enda scenario (deterministisk planering) bjuds planens volym in till vattenvärdet."""
    commitment = np.empty(len(price))
    for t in range(len(price)):
        order = np.argsort(scenario_prices[:, t])
        prices = scenario_prices[order, t]
        volumes = np.maximum.accumulate(scenario_power[order, t])
        floor = min(water_value, prices[0] - 1.0)
        if len(prices) == 1:
            commitment[t] = volumes[0] if price[t] >= floor else 0.0
        else:
            commitment[t] = np.interp(price[t], np.r_[floor, prices], np.r_[0.0, volumes])
    return commitment


def plan_day(strategy: Strategy, cascade: Cascade, day: pd.Timestamp, state: State, prices: pd.Series,
             inflows: pd.DataFrame, reserves: pd.DataFrame, wvs: MonthlyWaterValues,
             normal: pd.DataFrame | None = None):
    """Bud, clearing och leveransplan för ett dygn. normal är tillrinningens säsongsnormal för
    prognosen (None ger persistens). Returnerar (turbinflöde, spill, aggregat, åtagande i MW,
    sålda stödtjänster i MW, MIP-gap) per kvart."""
    wv = wvs.at(day)
    cascade = with_start_costs(cascade, strategy.start_cost_scale)
    steps = HORIZON_DAYS * STEPS_PER_DAY
    steady = cascade.steady_days(day, steps, STEP_H)
    known_at_bidding = prices[prices.index < day]
    inflow = inflow_path(inflows, day - DAY, day, steps, normal)
    actual = prices[day:day + DAY - pd.Timedelta(minutes=15)].to_numpy()
    reserve_price, reserve_cap = reserve_market(reserves, day, steps, known=strategy.kind == "perfekt")
    reserve_cap = reserve_cap * strategy.reserve_scale

    if strategy.kind == "regel":
        def rule_plan(min_load_blocks: bool) -> np.ndarray:
            if strategy.rule == "vattenvarde":
                # Stegbud efter vattenvärdet vid aktuell fyllnadsgrad. Budkurvan läggs dagen innan;
                # det verkliga priset avgör vilka kvartar som körs.
                segment = min(int(state.volume[0] // np.diff(wv.volumes)[0]), len(wv.volumes) - 2)
                water_value = wv.marginal(day)[segment] * strategy.water_value_scale
                return water_value_bid(cascade, actual, state, inflow[:STEPS_PER_DAY], targets(cascade, day + DAY, wv),
                                       water_value, STEP_H, min_load_blocks=min_load_blocks)
            if strategy.rule == "veckotopp":
                forecast, _ = price_path(known_at_bidding, day, steps)
                return weekly_peak(cascade, forecast, state, inflow, targets(cascade, day + HORIZON_DAYS * DAY, wv),
                                   STEP_H, min_load_blocks=min_load_blocks)
            forecast, _ = price_path(known_at_bidding, day, STEPS_PER_DAY)
            options = {} if strategy.rule == "jamn" else {"cap_downstream": strategy.cap_downstream}
            return RULES[strategy.rule](cascade, forecast, state, inflow[:STEPS_PER_DAY],
                                        targets(cascade, day + DAY, wv), STEP_H, min_load_blocks=min_load_blocks,
                                        **options)

        # Små flöden körs som block på minsta last och jämnas ut i dammen nedströms. Planen provkörs
        # mot prognosen med vattenhushållningsbestämmelserna; räcker dammen inte som buffert släpps
        # flödet förbi turbinen i stället.
        for min_load_blocks in (True, False):
            turbine = rule_plan(min_load_blocks)
            expected, _ = execute(cascade, turbine, np.zeros_like(turbine), None, inflow[:STEPS_PER_DAY], state,
                                  STEP_H, steady[:STEPS_PER_DAY])
            if buffer_holds(cascade, expected):
                break
        offer = hourly_offer(reserve_room(cascade, expected), reserve_cap)
        return turbine, np.zeros_like(turbine), None, expected.power.sum(axis=1), offer, 0.0

    if strategy.kind == "perfekt":
        window = slice(day, day + pd.Timedelta(hours=HORIZON_DAYS * 24) - pd.Timedelta(minutes=15))
        price = prices[window].to_numpy()
        true_inflow = inflows[window].to_numpy()[:len(price)]
        end = day + pd.Timedelta(hours=len(price) * STEP_H)
        plan = optimize(cascade, price, true_inflow, state, terminal(cascade, end, wv), STEP_H,
                        reserve_price=reserve_price[:len(price)], reserve_cap=reserve_cap, mip_gap=0.005,
                        steady=steady)
        return (plan.flow[:STEPS_PER_DAY], plan.spill[:STEPS_PER_DAY], plan.units[:STEPS_PER_DAY],
                plan.power[:STEPS_PER_DAY].sum(axis=1), plan.reserves[:STEPS_PER_DAY], plan.gap)

    term = terminal(cascade, day + HORIZON_DAYS * DAY, wv, strategy.follow_guide, strategy.water_value_scale)
    if strategy.scenarios == 1:
        scenarios = price_path(known_at_bidding, day, steps)[0][None, :]
    else:
        scenarios = price_scenarios(known_at_bidding, day, steps, strategy.scenarios)
    reserve_options = dict(reserve_price=reserve_price, reserve_cap=reserve_cap, steady=steady)
    # mFRR-kapacitet auktioneras före day-ahead och måste därför vara densamma i alla prisscenarier,
    # och produktionen första dygnet ska gå att lägga som en budkurva. Tvåstegsmodellen väger
    # reservintäkten mot vad den kostar: nedreglering kräver att man producerar även när priset
    # blir lågt, uppreglering att man avstår när det blir högt.
    plans = optimize_scenarios(cascade, scenarios, scenario_weights(len(scenarios)), inflow, state, term, STEP_H,
                               shared=MFRR, shared_steps=STEPS_PER_DAY, bid_steps=STEPS_PER_DAY,
                               **reserve_options)
    sold = plans[0].reserves[:STEPS_PER_DAY].copy()
    sold[:, FCR] = np.nan
    power = np.array([p.power[:STEPS_PER_DAY].sum(axis=1) for p in plans])
    water_value = term.marginal(0, state.volume[0]) / cascade.downstream_energy()[0]
    commitment = clear_bids(scenarios[:, :STEPS_PER_DAY], power, actual, water_value)
    price, _ = price_path(prices[prices.index < day + DAY], day, steps)
    # FCR auktioneras dagen före men efter day-ahead och säljs därför med leveransplanen, när
    # åtagandet är känt.
    plan = optimize(cascade, price, inflow, state, term, STEP_H, commitment=commitment, reserve_fixed=sold,
                    **reserve_options)
    sold = np.where(np.isnan(sold), plan.reserves[:STEPS_PER_DAY], sold)
    return (plan.flow[:STEPS_PER_DAY], plan.spill[:STEPS_PER_DAY], plan.units[:STEPS_PER_DAY], commitment, sold,
            plan.gap)


MODEL_FILES = ("backtest.py", "cascade.py", "data.py", "forecast.py", "heuristics.py", "model.py",
               "simulation.py", "watervalues.py")


def _code_version() -> str:
    """Kort hash av koden som påverkar resultatet, så att sparpunkter från äldre kod inte används."""
    return hashlib.sha1(b"".join((Path(__file__).parent / f).read_bytes() for f in MODEL_FILES)).hexdigest()[:12]


def run_strategy(strategy: Strategy, cascade: Cascade, start: pd.Timestamp, n_days: int, prices: pd.Series,
                 inflows: pd.DataFrame, reserves: pd.DataFrame, wvs: MonthlyWaterValues,
                 risk_wvs: MonthlyWaterValues | None = None, normal: pd.DataFrame | None = None) -> Result:
    """Kör en strategi dygn för dygn. Alla börjar på samma styrkurva (wvs); strategier med
    riskaverta vattenvärden planerar sedan med risk_wvs. normal är tillrinningens säsongsnormal
    för prognosen."""
    path = CHECKPOINTS / f"{strategy.key}_{start:%Y%m%d}_{n_days}d_{_code_version()}.pkl"
    if path.exists():
        done, state, days, gaps = pickle.loads(path.read_bytes())
        if done < n_days:
            print(f"  {strategy.label}: fortsätter från vecka {done // 7 + 1}", flush=True)
    else:
        done, state, days, gaps = 0, initial_state(cascade, start, inflows, wvs.at(start)), [], []
    plan_wvs = risk_wvs if strategy.risk_averse else wvs

    for i in range(done, n_days):
        day = start + i * DAY
        turbine, spill, units, commitment, sold, gap = plan_day(strategy, cascade, day, state, prices, inflows,
                                                                reserves, plan_wvs, normal)
        true_inflow = inflows.loc[day:day + DAY].to_numpy()[:STEPS_PER_DAY]
        outcome, state = execute(cascade, turbine, spill, units, true_inflow, state, STEP_H,
                                 cascade.steady_days(day, STEPS_PER_DAY, STEP_H))
        days.append((outcome, commitment, sold))
        gaps.append(gap)
        if (i + 1) % 7 == 0 or i + 1 == n_days:
            CHECKPOINTS.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps((i + 1, state, days, gaps)))
            for attempt in range(10):
                try:
                    tmp.replace(path)
                    break
                except PermissionError:   # filen kan vara låst en kort stund, t.ex. av antivirus
                    if attempt == 9:
                        raise
                    time.sleep(1 + attempt)
            if strategy.kind != "regel":
                print(f"  {strategy.label}: vecka {(i + 1 + 6) // 7} av {(n_days + 6) // 7} klar", flush=True)

    joined = Outcome(*(np.vstack([getattr(o, f) for o, _, _ in days]) for f in Outcome.__dataclass_fields__))
    return Result(joined, np.concatenate([c for _, c, _ in days]), np.vstack([r for _, _, r in days]), state, gaps)


def run_all(strategies, cascade: Cascade, start: pd.Timestamp, n_days: int, prices: pd.Series,
            inflows: pd.DataFrame, reserves: pd.DataFrame, wvs: MonthlyWaterValues,
            risk_wvs: MonthlyWaterValues | None = None, normal: pd.DataFrame | None = None,
            parallel: bool = True) -> dict[str, Result]:
    args = (cascade, start, n_days, prices, inflows, reserves, wvs, risk_wvs, normal)
    if not parallel:
        return {s.key: run_strategy(s, *args) for s in strategies}
    # De tunga optimeringarna först; tumreglerna går fort och fyller ut när kärnor blir lediga.
    ordered = sorted(strategies, key=lambda s: s.kind == "regel")
    with ProcessPoolExecutor(max_workers=min(len(strategies), os.cpu_count() or 1)) as pool:
        futures = {s.key: pool.submit(run_strategy, s, *args) for s in ordered}
        return {s.key: futures[s.key].result() for s in strategies}


def period_values(cascade: Cascade, result: Result, spot: np.ndarray, imbalance: np.ndarray,
                  reserve_price: np.ndarray, start_cost_scale: float = 1.0) -> dict[str, np.ndarray]:
    """Intäkter och kostnader per period. Åtagandet betalas med spotpriset, avvikelser med
    obalanspriset och sålda stödtjänster med kapacitetspriset för den del som gick att leverera."""
    o = result.outcome
    produced = o.power.sum(axis=1)
    delivered = deliverable_reserves(result.reserves, reserve_room(cascade, o))
    costs = np.array([p.unit.start_cost for p in cascade.plants]) * start_cost_scale
    return {
        "energy": (spot * result.commitment + imbalance * (produced - result.commitment)) * STEP_H,
        "imbalance": (imbalance - spot) * (produced - result.commitment) * STEP_H,
        "reserves": (reserve_price * delivered).sum(axis=1) * STEP_H,
        "reserves_missed": (result.reserves - delivered).sum(axis=1) * STEP_H,
        "start_costs": (o.starts * costs).sum(axis=1),
    }


def evaluate(cascade: Cascade, result: Result, spot: np.ndarray, imbalance: np.ndarray, reserve_price: np.ndarray,
             end: pd.Timestamp, wv: WaterValues, water_value_scale: float = 1.0,
             start_cost_scale: float = 1.0) -> dict[str, float]:
    """Nyckeltal. Vattnet som är kvar vid periodens slut värderas med samma vattenvärdesfunktion
    för alla strategier, så att ingen vinner på att tömma magasinen."""
    o = result.outcome
    values = {k: float(v.sum()) for k, v in period_values(cascade, result, spot, imbalance, reserve_price,
                                                          start_cost_scale).items()}
    water = terminal(cascade, end, wv, scale=water_value_scale).value(result.final_state.volume)
    produced = o.power.sum(axis=1)
    energy = produced.sum() * STEP_H
    return {
        "energy_gwh": energy / 1000,
        "capture_price": values["energy"] / energy,
        "energy_revenue": values["energy"],
        "reserve_revenue": values["reserves"],
        "revenue": values["energy"] + values["reserves"],
        "imbalance": values["imbalance"],
        "reserves_missed_mwh": values["reserves_missed"],
        "deviation_gwh": float(np.abs(produced - result.commitment).sum() * STEP_H / 1000),
        "starts": o.starts.sum(),
        "start_costs": values["start_costs"],
        "spill_mm3": o.spill.sum() * 0.0036 * STEP_H,
        "min_flow_breach_h": float((o.shortfall > 0.5).any(axis=1).sum() * STEP_H),
        "water_value": water,
        "net": values["energy"] + values["reserves"] - values["start_costs"] + water,
    }
