"""Korttidsplanering av Trängslet, Åsen och Spjutmo med riktiga priser och tillrinning.

    python run.py                    # backtest de senaste 4 veckorna
    python run.py --weeks 52         # backtest ett helt år (avbruten körning fortsätter där den slutade)
    python run.py --weeks 52 --sensitivity   # även känslighetsanalys (vattenvärde, startkostnad)
    python run.py --weeks 52 --slut 2026-10-12   # en bestämd period i stället för den senaste
    python run.py --plan             # körplan för i morgon med publicerade spotpriser
    python run.py --plan --magasin 700,1.5,2   # utgå från aktuella volymer (Mm³)

Priser: Nord Pool day-ahead (via elprisetjustnu.se och Energi Data Service), obalanspriser (eSett)
och stödtjänstpriser (Svenska kraftnät, Mimer). Tillrinning: SMHI.
Resultat (tabeller och figurer) sparas i resultat/.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import plot
from backtest import (DAY, HORIZON_DAYS, SENSITIVITY, STEP_H, STEPS_PER_DAY, STRATEGIES, evaluate,
                      initial_state, period_values, reserve_market, run_all, terminal)
from cascade import OSTERDALALVEN, Cascade
from data import (CET, RESERVES, daily_inflows, last_runoff_date, load_imbalance_prices, load_prices,
                  load_regulated_flow, load_reserve_prices, load_runoff, load_unit_generation, local_inflows)
from forecast import inflow_normal, inflow_path, price_path
from model import State, optimize
from simulation import Outcome
from watervalues import RISK_AVERSE, cached_water_values, monthly_water_values

OUTPUT_DIR = Path(__file__).parent / "resultat"
NORMAL_YEAR_GWH = {"Trängslet": 651, "Åsen": 117, "Spjutmo": 138}   # Fortums normalårsproduktion
BOOTSTRAP_BLOCK_WEEKS = 4


def fmt(x: float, decimals: int = 0) -> str:
    """Svensk tusentalsavgränsning: 12 345."""
    return f"{x:,.{decimals}f}".replace(",", " ")


def signed(x: float) -> str:
    """Som fmt, men med tecken även för positiva tal: +12 345."""
    return ("+" if x >= 0 else "") + fmt(x)


def pct(x: float) -> str:
    """Andel med tecken och decimalkomma: +15,5 %."""
    return f"{x:+.1%}".replace(".", ",").replace("%", " %")


def monday(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.normalize() - pd.Timedelta(days=ts.weekday())


def now_cet() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_convert(CET).tz_localize(None)


def bootstrap_interval(weekly: np.ndarray, block: int = BOOTSTRAP_BLOCK_WEEKS, draws: int = 10_000,
                       seed: int = 1) -> tuple[float, float]:
    """95 %-intervall för summan av veckovärdena med block-bootstrap (block om några veckor,
    eftersom närliggande veckor liknar varandra)."""
    rng = np.random.default_rng(seed)
    block = min(block, len(weekly))
    n_blocks = int(np.ceil(len(weekly) / block))
    starts = rng.integers(0, len(weekly) - block + 1, size=(draws, n_blocks))
    sums = np.array([np.concatenate([weekly[s:s + block] for s in row])[:len(weekly)].sum() for row in starts])
    return tuple(np.percentile(sums, [2.5, 97.5]))


def backtest(cascade: Cascade, weeks: int, area: str, sensitivity: bool, last: str | None = None,
             actual: bool = False) -> None:
    today = now_cet().normalize()
    first = (monday(pd.Timestamp(last)) if last else today) - pd.Timedelta(weeks=weeks)
    prices = load_prices(first - pd.Timedelta(days=60), today + 2 * DAY, area)
    runoff = load_runoff()
    imbalance_all = load_imbalance_prices(first - pd.Timedelta(days=14), today, area)
    reserves = load_reserve_prices(first - pd.Timedelta(days=60), today, area)
    # Perfekt information behöver facit sju dygn efter sista planeringsdygnet.
    end = monday(min(prices.index[-1] - HORIZON_DAYS * DAY, last_runoff_date(runoff) - HORIZON_DAYS * DAY,
                     imbalance_all.index[-1], reserves.index[-1] - HORIZON_DAYS * DAY))
    if last:
        end = min(end, monday(pd.Timestamp(last)))
    start = end - pd.Timedelta(weeks=weeks)
    inflows = local_inflows(cascade, start - DAY, end + HORIZON_DAYS * DAY, runoff)
    period = pd.date_range(start, end, freq="15min", inclusive="left")
    spot = prices.reindex(period).to_numpy()
    imbalance = imbalance_all.reindex(period).fillna(prices.reindex(period)).to_numpy()
    reserve_price = reserve_market(reserves, start, len(period), known=True)[0]

    print(f"Backtest {start:%Y-%m-%d} – {end - DAY:%Y-%m-%d} ({weeks} veckor), {area}, {cascade.name} "
          f"({sum(p.p_max for p in cascade.plants):.0f} MW).")
    print("Räknar vattenvärden med SDP för varje månad, med den data som fanns då ...", flush=True)
    wvs = monthly_water_values(cascade, start, end + HORIZON_DAYS * DAY, area)
    strategies = STRATEGIES + (SENSITIVITY if sensitivity else ())
    risk_wvs = (monthly_water_values(cascade, start, end + HORIZON_DAYS * DAY, area, RISK_AVERSE)
                if any(s.risk_averse for s in strategies) else None)
    normal = inflow_normal(daily_inflows(cascade, runoff), start)   # bara år före backtesten
    print(f"Kör {len(strategies)} strategier parallellt ...", flush=True)
    results = run_all(strategies, cascade, start, weeks * 7, prices, inflows, reserves, wvs, risk_wvs, normal)
    labels = {s.key: s.label for s in strategies}
    wv_end = wvs.at(end - DAY)
    summary = pd.DataFrame({s.key: evaluate(cascade, results[s.key], spot, imbalance, reserve_price, end, wv_end)
                            for s in STRATEGIES}).T

    # Värde per dygn: intäkt minus startkostnader plus förändringen i vattnets värde under dygnet.
    days = pd.date_range(start, end, freq="D", inclusive="left")
    first_value = terminal(cascade, start, wvs.at(start)).value(initial_state(cascade, start, inflows, wvs.at(start)).volume)
    daily = {}
    for s in STRATEGIES:
        r = results[s.key]
        values = period_values(cascade, r, spot, imbalance, reserve_price)
        net = values["energy"] + values["reserves"] - values["start_costs"]
        volumes = r.outcome.volume[STEPS_PER_DAY - 1::STEPS_PER_DAY]   # vid varje dygns slut
        stock = np.array([first_value] + [terminal(cascade, d + DAY, wvs.at(d)).value(v) for d, v in zip(days, volumes)])
        daily[s.key] = pd.Series(net.reshape(-1, STEPS_PER_DAY).sum(axis=1) + np.diff(stock), index=days)
    weekly = pd.DataFrame({labels[k]: v.to_numpy().reshape(-1, 7).sum(axis=1) for k, v in daily.items()},
                          index=days[::7])

    print(f"\nSpotpris i snitt {np.mean(spot):.1f} €/MWh, tillrinning till Trängslet i snitt "
          f"{inflows.loc[start:end].iloc[:, 0].mean():.0f} m³/s (SMHI Idre 3, skalad efter area)\n")
    print(f"{'Strategi':<29}{'Energi':>8}{'Snittpris':>10}{'El':>8}{'Stödtj.':>9}{'Obalans':>9}{'Starter':>9}"
          f"{'Spill':>7}{'Magasin':>9}{'Vatten*':>9}{'Netto':>9}")
    print(f"{'':<29}{'GWh':>8}{'€/MWh':>10}{'M€':>8}{'M€':>9}{'k€':>9}{'':>9}{'Mm³':>7}{'Mm³':>9}{'M€':>9}{'M€':>9}")
    for key, s in summary.iterrows():
        print(f"{labels[key]:<29}{s.energy_gwh:>8.1f}{s.capture_price:>10.1f}{s.energy_revenue / 1e6:>8.2f}"
              f"{s.reserve_revenue / 1e6:>9.2f}{s.imbalance / 1e3:>9.0f}{fmt(s.starts):>9}{s.spill_mm3:>7.1f}"
              f"{results[key].final_state.volume[0]:>9.0f}{s.water_value / 1e6:>9.2f}{s.net / 1e6:>9.2f}")
    print("* Värdet av vattnet som är kvar vid periodens slut, med samma vattenvärden för alla strategier.")
    print("  El: spot plus obalans. Stödtj.: FCR och mFRR, bara den del som gick att leverera.")
    print("  Obalans: vad avvikelserna mot budet kostade (-) eller gav (+) jämfört med att sälja till spotpris.")
    for key, s in summary.iterrows():
        if s.reserves_missed_mwh > 1:
            print(f"! {labels[key]}: {fmt(s.reserves_missed_mwh)} MWh såld stödtjänst gick inte att leverera.")
    for key, s in summary.iterrows():
        if s.min_flow_breach_h:
            print(f"! {labels[key]}: minimitappningen klarades inte under {s.min_flow_breach_h:.0f} timmar.")

    # Jämförelserna görs mot den tumregel som gick bäst, så att vinsten inte överskattas:
    # korttidsvinsten mot regler som följer samma styrkurva, totalvinsten mot alla regler.
    rules = [s for s in STRATEGIES if s.kind == "regel"]
    base_guide = max((s.key for s in rules if s.rule != "vattenvarde"), key=lambda k: summary.loc[k].net)
    base = max((s.key for s in rules), key=lambda k: summary.loc[k].net)
    rule = summary.loc[base]
    gain = summary.loc["milp"].net - rule.net
    short_term = summary.loc["styrkurva"].net - summary.loc[base_guide].net
    seasonal = summary.loc["milp"].net - summary.loc["styrkurva"].net
    low, high = bootstrap_interval((weekly[labels["styrkurva"]] - weekly[labels[base_guide]]).to_numpy())
    print(f"\nKorttidsplanering, samma styrkurva: MILP som följer styrkurvan mot {labels[base_guide].lower()}")
    print(f"  {fmt(short_term / 1e3):>7} k€  ({short_term / summary.loc[base_guide].revenue:+.1%} av regelns intäkt), "
          f"95 % intervall {fmt(low / 1e3)} till {fmt(high / 1e3)} k€")
    print("Säsongsplanering med vattenvärdena: MILP med budgivning mot MILP som följer styrkurvan")
    print(f"  {fmt(seasonal / 1e3):>7} k€  (beror på vattenvärdena, se känslighetsanalys)")
    print(f"Totalt: MILP med budgivning mot bästa tumregeln ({labels[base].lower()})")
    print(f"  {fmt(gain / 1e3):>7} k€  ({gain / rule.revenue:+.1%} av regelns intäkt)")
    scenario_value = summary.loc["milp"].net - summary.loc["deterministisk"].net
    print("Prisscenarier och budkurva: MILP med budgivning mot MILP med punktprognos")
    print(f"  {fmt(scenario_value / 1e3):>7} k€")
    print(f"Perfekt information hade gett:     {fmt((summary.loc['perfekt'].net - summary.loc['milp'].net) / 1e3):>7} k€ "
          f"till (värdet av att känna priser och tillrinning i förväg)")
    gains = weekly[labels["milp"]] - weekly[labels[base]]
    print(f"Veckovis: MILP med budgivning bättre än bästa tumregeln {(gains > 0).sum()} av {weeks} veckor")
    gaps = np.array([g for k in ("milp", "deterministisk", "styrkurva", "perfekt") for g in results[k].mip_gaps])
    print(f"MIP-gap i de {len(gaps)} leveransoptimeringarna: median {np.median(gaps):.2%}, största {gaps.max():.2%}, "
          f"över 1 % i {(gaps > 0.01).sum()}")

    sensitivity_rows = []
    if sensitivity:
        print("\nKänslighet, MILP mot bästa tumregeln med samma antagande (lägsta nivå i Trängslet under året):")
        all_rules = [s for s in strategies if s.kind == "regel"]
        for s in (s for s in SENSITIVITY if s.kind == "milp"):
            kw = dict(water_value_scale=s.water_value_scale, start_cost_scale=s.start_cost_scale)
            # Samma stödtjänsttak för alla. Vattenvärdesbudet beslutar med vattenvärdena och tas med
            # samma skalning och risk som MILP; en MILP som följer styrkurvan jämförs bara med
            # regler som också gör det.
            candidates = [r for r in all_rules if r.reserve_scale == s.reserve_scale and (
                r.rule != "vattenvarde" if s.follow_guide else
                r.rule != "vattenvarde" or (r.water_value_scale, r.risk_averse) == (s.water_value_scale, s.risk_averse))]
            scored = {r.key: evaluate(cascade, results[r.key], spot, imbalance, reserve_price, end, wv_end, **kw)
                      for r in candidates}
            best = max(scored, key=lambda k: scored[k]["net"])
            g = (evaluate(cascade, results[s.key], spot, imbalance, reserve_price, end, wv_end, **kw)["net"]
                 - scored[best]["net"])
            lowest = results[s.key].outcome.volume[:, 0].min()
            sensitivity_rows.append((s.label, g, g / scored[best]["revenue"], labels[best], lowest))
            print(f"  {s.label:<38} {fmt(g / 1e3):>7} k€  ({g / scored[best]['revenue']:+.1%}) "
                  f"mot {labels[best].lower()}  ({lowest:.0f} Mm³)")
        print(f"  (MILP med budgivning: lägsta nivå {results['milp'].outcome.volume[:, 0].min():.0f} Mm³)")

    print("\nProduktion per kraftverk (GWh) jämfört med Fortums normalår:")
    normal = daily_inflows(cascade, runoff).loc["1991":"2020"].iloc[:, 0].mean()
    ratio = inflows.loc[start:end].iloc[:, 0].mean() / normal
    for p, plant in enumerate(cascade.plants):
        cells = "  ".join(f"{labels[k]} {results[k].outcome.power[:, p].sum() * STEP_H / 1000 * 52 / weeks:6.0f}"
                          for k in (base, "milp"))
        print(f"  {plant.name:<10} normalår {NORMAL_YEAR_GWH.get(plant.name, float('nan')):>4}   {cells}")
    print(f"  (tillrinningen i perioden var {ratio:.0%} av normal 1991–2020; omräknat till helår)")

    OUTPUT_DIR.mkdir(exist_ok=True)
    compare_with_regulated_flow(cascade, runoff, period, results, labels, (base_guide, "styrkurva", "milp"),
                                wv_end)
    weekly.to_csv(OUTPUT_DIR / "backtest_veckor.csv", float_format="%.0f")
    summary.rename(index=labels).to_csv(OUTPUT_DIR / "backtest_summering.csv", float_format="%.2f")
    if sensitivity_rows:
        pd.DataFrame(sensitivity_rows, columns=["antagande", "merintakt_eur", "andel", "basta_tumregel",
                                                "lagsta_magasin_mm3"]).to_csv(
            OUTPUT_DIR / "kanslighet.csv", index=False, float_format="%.3f")

    cumulative = {k: (daily[k] - daily[base]).cumsum() for k in ("perfekt", "milp", "styrkurva")}
    headline = (f"MILP med budgivning: {signed(gain / 1e3)} k€ ({pct(gain / rule.revenue)}) mot bästa tumregeln "
                f"på {weeks} veckor")
    plot.backtest_overview(cumulative,
                           {k: pd.Series(results[k].outcome.volume[STEPS_PER_DAY - 1::STEPS_PER_DAY, 0], index=days)
                            for k in (base, "styrkurva", "milp")},
                           pd.Series([wvs.at(d).target(d) for d in days], index=days),
                           pd.Series(spot, index=period).resample("D").mean(), labels, headline,
                           OUTPUT_DIR / "backtest.png")
    plot.water_values(wv_end, cascade, f"Vattenvärden och styrkurva för Trängslet (SDP, data t.o.m. "
                      f"{end - DAY:%Y-%m-%d})", OUTPUT_DIR / "vattenvarden.png")
    week_gain = weekly[labels["styrkurva"]] - weekly[labels[base_guide]]
    median_week = (week_gain - week_gain.median()).abs().idxmin()
    rows = slice(period.get_loc(median_week), period.get_loc(median_week) + 7 * STEPS_PER_DAY)
    week_outcomes = {k: Outcome(*(getattr(results[k].outcome, f)[rows] for f in Outcome.__dataclass_fields__))
                     for k in (base_guide, "styrkurva")}
    plot.detail_week(period[rows], spot[rows], week_outcomes, cascade, labels,
                     f"Typisk vecka ({median_week:%Y-%m-%d}): MILP som följer styrkurvan "
                     f"{signed(week_gain[median_week] / 1e3)} k€ mot {labels[base_guide].lower()}",
                     OUTPUT_DIR / "vecka.png")
    if actual:
        compare_with_actual(cascade, period, spot,
                            {k: results[k].outcome.power[:, 0] for k in (base_guide, "styrkurva", "milp")},
                            labels, median_week, area)
    print(f"\nFigurer och tabeller i {OUTPUT_DIR}")


def drawdowns(actual: pd.Series, natural: pd.Series) -> pd.Series:
    """Hur mycket det övre magasinet tappades ur före vårfloden varje år (Mm³), ur det uppmätta
    flödet vid sista kraftverket: största kumulativa överskottet av tappning över naturlig
    tillrinning från 1 oktober till 1 juli. Dammarna nedströms är små och försummas."""
    both = pd.concat([actual.rename("a"), natural.rename("n")], axis=1).dropna()
    out = {}
    for year in range(both.index[0].year, both.index[-1].year):
        window = both[f"{year}-10-01":f"{year + 1}-06-30"]
        if len(window) > 250:
            out[year + 1] = ((window.a - window.n) * 86400 / 1e6).cumsum().max()
    return pd.Series(out, dtype=float)


def compare_with_regulated_flow(cascade: Cascade, runoff: pd.Series, period: pd.DatetimeIndex, results: dict,
                                labels: dict[str, str], keys: tuple[str, ...], wv) -> None:
    """Modellen mot Fortums verkliga tappning: dygnsflödet vid Spjutmo kraftverk (SMHI) är det som
    tappats genom kaskaden plus den lokala tillrinningen, och samma sak räknas ur modellen."""
    try:
        actual = load_regulated_flow()
    except RuntimeError as err:
        print(f"\nVerklig tappning: {err}")
        return
    natural = daily_inflows(cascade, runoff).sum(axis=1)
    last = cascade.plants[-1]
    print(f"\nVerklig tappning vid {last.name} (SMHI) jämfört med modellen:")
    years = drawdowns(actual, natural)
    guide = wv.guide[:, 1]
    print(f"  Uttappning av {cascade.plants[0].name} före vårfloden {years.index[0]}–{years.index[-1]}: median "
          f"{years.median():.0f} Mm³, {(years > 0.8 * cascade.plants[0].v_max).mean():.0%} av åren mer än 80 % av "
          f"magasinet; styrkurvan tappar ur {guide.max() - guide.min():.0f} Mm³")
    days = pd.date_range(period[0], min(period[-1].normalize(), actual.dropna().index[-1]))
    if len(days) < 7:
        print("  Inga uppmätta flöden under backtesten.")
        return
    flows = {"Verklig drift": actual.reindex(days)}
    for k in keys:
        o = results[k].outcome
        release = pd.Series(o.flow[:, -1] + o.spill[:, -1], index=period)
        flows[labels[k]] = release.resample("D").mean().reindex(days)
    table = pd.DataFrame(flows)
    total = table.sum() * 86400 / 1e6
    print(f"  {days[0]:%Y-%m-%d} – {days[-1]:%Y-%m-%d}, medelflöde per månad (m³/s):")
    monthly = table.groupby(table.index.to_period("M")).mean()
    print("  " + monthly.round(0).to_string().replace("\n", "\n  "))
    print("  Total tappning (Mm³): " + ", ".join(f"{k} {v:.0f}" for k, v in total.items()))
    corr = {k: table["Verklig drift"].corr(table[k]) for k in table if k != "Verklig drift"}
    print("  Korrelation med verklig tappning per dygn: " + ", ".join(f"{k} {v:.2f}" for k, v in corr.items()))
    colors = {"Verklig drift": plot.INK, **{labels[k]: plot.COLORS[k] for k in keys}}
    plot.regulated_flow(table, colors, f"Flöde vid {last.name}: modellen jämfört med Fortums verkliga tappning",
                        OUTPUT_DIR / "verklig_tappning.png")


def compare_with_actual(cascade: Cascade, period: pd.DatetimeIndex, spot: np.ndarray, model: dict[str, np.ndarray],
                        labels: dict[str, str], week: pd.Timestamp, area: str) -> None:
    """Trängslets produktion i modellen jämfört med den verkliga enligt ENTSO-E. Magasinet och
    tillrinningen skiljer sig mellan modell och verklighet, så energin är inte direkt jämförbar,
    men snittpriset och hur produktionen fördelas över dyra och billiga timmar är det."""
    name = cascade.plants[0].name
    print(f"\nJämförelse med {name}s verkliga produktion (ENTSO-E):")
    try:
        units = load_unit_generation(period[0], period[-1] + pd.Timedelta(minutes=15), name, area)
    except RuntimeError as err:
        print(f"  {err}")
        return
    if units.empty or units.notna().sum().sum() == 0:
        print(f"  Hittade ingen produktion för {name} i svaret från ENTSO-E.")
        return
    actual = units.sum(axis=1, min_count=1).to_numpy()
    known = ~np.isnan(actual)
    print(f"  Aggregat: {', '.join(units.columns)}; data för {known.mean():.0%} av kvartarna.")
    expensive = spot >= np.nanpercentile(spot[known], 75)
    rows = {"Verklig drift": actual, **{labels[k]: v for k, v in model.items()}}
    colors = {"Verklig drift": plot.INK, **{labels[k]: plot.COLORS[k] for k in model}}
    print(f"  {'':<29}{'Energi':>9}{'Snittpris':>11}{'I dyraste kvartilen':>21}{'Korrelation med pris':>22}")
    for label, power in rows.items():
        p = np.where(known, power, 0.0)
        print(f"  {label:<29}{p.sum() * STEP_H / 1000:>7.0f} GWh{(spot * p).sum() / p.sum():>7.1f} €/MWh"
              f"{p[expensive].sum() / p.sum():>20.0%}{np.corrcoef(p[known], spot[known])[0, 1]:>22.2f}")
    monthly = pd.DataFrame({label: pd.Series(np.where(known, power, 0.0) * spot, index=period).resample("MS").sum()
                            / pd.Series(np.where(known, power, 0.0), index=period).resample("MS").sum()
                            for label, power in rows.items()})
    in_week = slice(period.get_loc(week), period.get_loc(week) + 7 * STEPS_PER_DAY)
    shown = ("Verklig drift", labels["styrkurva"])
    plot.actual_comparison(period[in_week], spot[in_week], {label: rows[label][in_week] for label in shown},
                           monthly, colors, f"{name}: modellen jämfört med verklig drift",
                           OUTPUT_DIR / "verklig_drift.png")


def day_ahead_plan(cascade: Cascade, area: str, volumes: list[float] | None) -> None:
    now = now_cet()
    prices = load_prices(now.normalize() - pd.Timedelta(days=14), now.normalize() + 2 * DAY, area)
    tomorrow = now.normalize() + DAY
    day = tomorrow if prices.index[-1] >= tomorrow + pd.Timedelta(hours=20) else now.normalize()
    runoff = load_runoff(max_age_hours=1)
    last = last_runoff_date(runoff)
    inflows = local_inflows(cascade, last, day + 8 * DAY, runoff)
    wv = cached_water_values(cascade, day.replace(day=1), area)

    steps = HORIZON_DAYS * STEPS_PER_DAY
    price, n_known = price_path(prices, day, steps)
    inflow = inflow_path(inflows, last, day, steps, inflow_normal(daily_inflows(cascade, runoff), last))
    reserves = load_reserve_prices(now.normalize() - pd.Timedelta(days=35), day, area)
    reserve_price, reserve_cap = reserve_market(reserves, day, steps, known=False)
    if volumes is None:
        state = initial_state(cascade, day, inflows, wv)
    else:
        natural = np.cumsum(inflow[0])
        state = State(np.array(volumes), np.tile(natural, (round(cascade.max_delay / STEP_H), 1)),
                      np.zeros(len(volumes), int))
    plan = optimize(cascade, price, inflow, state, terminal(cascade, day + HORIZON_DAYS * DAY, wv),
                    reserve_price=reserve_price, reserve_cap=reserve_cap,
                    steady=cascade.steady_days(day, steps, STEP_H))

    time = pd.date_range(day, periods=steps, freq="15min")
    print(f"Körplan för {day:%Y-%m-%d} (CET), {area}: {min(n_known, 96)} av 96 kvartar har publicerat spotpris"
          f"{'' if n_known >= STEPS_PER_DAY else ', resten prognos'}.")
    print(f"Tillrinning: senaste SMHI-dygn {last:%Y-%m-%d}, {inflow[0, 0]:.0f} m³/s till Trängslet. "
          f"Startvolymer {', '.join(f'{v:.1f}' for v in state.volume)} Mm³"
          f"{' (styrkurvan)' if volumes is None else ''}.")
    print(f"Vattenvärde vid styrkurvan: {wv.level(day, cascade.downstream_energy()[0]):.0f} €/MWh "
          f"(SDP med pris och tillrinning {wv.years[0]}–{wv.years[1]}, prisnivå {wv.price_level:.0f} €/MWh).\n")
    header = "".join(f"{p.name:>16}" for p in cascade.plants)
    print(f"{'Timme':<7}{'Pris':>8}{header}")
    print(f"{'':<7}{'€/MWh':>8}" + "".join(f"{'MW':>10}{'agg':>6}" for _ in cascade.plants))
    hourly_price = price[:STEPS_PER_DAY].reshape(24, 4).mean(axis=1)
    hourly_power = plan.power[:STEPS_PER_DAY].reshape(24, 4, -1).mean(axis=1).clip(min=0)   # utan "-0.0"
    hourly_units = plan.units[:STEPS_PER_DAY].reshape(24, 4, -1).max(axis=1)
    for h in range(24):
        cells = "".join(f"{hourly_power[h, p]:>10.1f}{hourly_units[h, p]:>6}" for p in range(len(cascade.plants)))
        marker = "" if (h + 1) * 4 <= n_known else "  (prognos)"
        print(f"{day + pd.Timedelta(hours=h):%H:%M}{hourly_price[h]:>10.1f}{cells}{marker}")
    revenue = (price[:STEPS_PER_DAY] * plan.power[:STEPS_PER_DAY].sum(axis=1)).sum() * STEP_H
    energy = plan.power[:STEPS_PER_DAY].sum() * STEP_H
    reserve_revenue = (reserve_price[:STEPS_PER_DAY] * plan.reserves[:STEPS_PER_DAY]).sum() * STEP_H
    names = {"fcr_n": "FCR-N", "fcr_d_upp": "FCR-D upp", "fcr_d_ned": "FCR-D ned", "mfrr_upp": "mFRR upp",
             "mfrr_ned": "mFRR ned"}
    print(f"\nFörväntad intäkt för dygnet: {fmt(revenue)} € från {energy:.0f} MWh el "
          f"(snittpris {revenue / max(energy, 1e-9):.1f} €/MWh) och {fmt(reserve_revenue)} € från stödtjänster.")
    print("Stödtjänster att sälja, MW i snitt över dygnet (prognospris €/MW per timme): " + ", ".join(
        f"{names[r]} {plan.reserves[:STEPS_PER_DAY, i].mean():.0f} ({reserve_price[:STEPS_PER_DAY, i].mean():.1f})"
        for i, r in enumerate(RESERVES)) + f". MIP-gap {plan.gap:.2%}.")

    OUTPUT_DIR.mkdir(exist_ok=True)
    table = pd.DataFrame({"price": price, "price_known": np.arange(steps) < n_known}, index=time)
    for p, plant in enumerate(cascade.plants):
        table[f"{plant.name} MW"] = plan.power[:, p]
        table[f"{plant.name} aggregat"] = plan.units[:, p]
        table[f"{plant.name} m3/s"] = plan.flow[:, p]
        table[f"{plant.name} Mm3"] = plan.volume[:, p]
    for i, r in enumerate(RESERVES):
        table[f"{names[r]} MW"] = plan.reserves[:, i]
    table.to_csv(OUTPUT_DIR / f"plan_{day:%Y-%m-%d}.csv", float_format="%.2f")
    fill = plan.volume / np.array([p.v_max for p in cascade.plants])
    plot.day_plan(time, price, n_known, plan.power, fill, cascade,
                  f"Körplan {day:%Y-%m-%d}: {fmt(revenue)} € förväntad intäkt (skuggat = preliminärt)",
                  OUTPUT_DIR / "plan.png")
    print(f"Plan och figur i {OUTPUT_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--weeks", type=int, default=4, help="antal veckor i backtesten")
    parser.add_argument("--area", default="SE3", help="elområde (SE1-SE4)")
    parser.add_argument("--sensitivity", action="store_true", help="kör även känslighetsanalysen")
    parser.add_argument("--slut", help="backtestens slut (YYYY-MM-DD, avrundas till måndag); "
                                       "annars senaste möjliga")
    parser.add_argument("--verklig", action="store_true",
                        help="jämför med Trängslets verkliga produktion (kräver nyckel till ENTSO-E)")
    parser.add_argument("--plan", action="store_true", help="gör en skarp körplan för i morgon")
    parser.add_argument("--magasin", help="aktuella volymer i Mm³, kommaseparerade uppströms -> nedströms")
    args = parser.parse_args()

    if args.plan:
        volumes = [float(v) for v in args.magasin.split(",")] if args.magasin else None
        day_ahead_plan(OSTERDALALVEN, args.area, volumes)
    else:
        backtest(OSTERDALALVEN, args.weeks, args.area, args.sensitivity, args.slut, args.verklig)


if __name__ == "__main__":
    main()
