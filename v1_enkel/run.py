"""Veckoplanering av ett vattenkraftverk: LP-optimering jämfört med tumregler.

    python run.py                    # syntetisk höstvecka (SE2-lik)
    python run.py --seed 7           # en annan slumpad vecka
    python run.py --data egen.csv    # egna timdata, kolumner: time, price, inflow
    python run.py --scenarios 100    # hur stor är vinsten över 100 slumpade veckor?

Resultat (körplan, indata och figur) sparas i mappen resultat/.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from data import load_csv, synthetic_week
from heuristics import daily_peak, even_release, simulate
from model import MM3_PER_M3S_HOUR, Plant, optimize
from plot import plot_week

OUTPUT_DIR = Path(__file__).parent / "resultat"
LABELS = {"jamn": "Jämn tappning", "dygnstopp": "Dygnstopp (tumregel)", "lp": "LP-optimering"}
RULES = {"jamn": even_release, "dygnstopp": daily_peak}
WEEKS_PER_YEAR = 52


def run_week(df: pd.DataFrame, plant: Plant) -> dict[str, pd.DataFrame]:
    price, inflow = df.price.to_numpy(), df.inflow.to_numpy()
    results = {key: simulate(rule(price, inflow, plant), inflow, plant) for key, rule in RULES.items()}
    results["lp"] = optimize(price, inflow, plant)
    return results


def summarize(results: dict[str, pd.DataFrame], price: np.ndarray, plant: Plant) -> pd.DataFrame:
    """Nyckeltal per strategi. Om en tumregel inte landar exakt på slutmålet (för att
    magasinet slog i en gräns) värderas skillnaden till LP:ns vattenvärde, så att
    alla jämförs på samma mängd vatten."""
    end_water_value = results["lp"].water_value.iloc[-1] * plant.energy_equivalent  # €/Mm³
    rows = {}
    for key, r in results.items():
        revenue = float((price * r.power).sum())
        rows[key] = {
            "energy": r.power.sum(),
            "capture_price": revenue / r.power.sum(),
            "revenue": revenue,
            "spill": r.spill.sum() * MM3_PER_M3S_HOUR,
            "end_volume": r.volume.iloc[-1],
            "value": revenue + (r.volume.iloc[-1] - plant.v_end) * end_water_value,
        }
    return pd.DataFrame(rows).T


def fmt(x: float, decimals: int = 0) -> str:
    """Svensk tusentalsavgränsning: 12 345."""
    return f"{x:,.{decimals}f}".replace(",", " ")


def report(df: pd.DataFrame, results: dict[str, pd.DataFrame], summary: pd.DataFrame,
           plant: Plant) -> str:
    print(f"\nVecka {df.index[0]:%Y-%m-%d} – {df.index[-1]:%Y-%m-%d}  |  "
          f"snittpris {df.price.mean():.1f} €/MWh  |  tillrinning {df.inflow.mean():.0f} m³/s i snitt")
    print(f"Kraftverk {plant.p_max:.0f} MW ({plant.q_max:.0f} m³/s), magasin "
          f"{plant.v_min:g}–{plant.v_max:g} Mm³, start/mål {plant.v_start:g}/{plant.v_end:g} Mm³, "
          f"minimitappning {plant.min_flow:g} m³/s\n")

    print(f"{'Strategi':<22}{'Energi':>9}{'Snittpris':>11}{'Intäkt':>11}{'Spill':>8}{'Slutmagasin':>13}")
    print(f"{'':<22}{'MWh':>9}{'€/MWh':>11}{'€':>11}{'Mm³':>8}{'Mm³':>13}")
    for key in ("jamn", "dygnstopp", "lp"):
        s = summary.loc[key]
        print(f"{LABELS[key]:<22}{fmt(s.energy):>9}{s.capture_price:>11.1f}{fmt(s.revenue):>11}"
              f"{s.spill:>8.2f}{s.end_volume:>13.2f}")
    if not np.allclose(summary.value, summary.revenue, atol=1.0):
        print("* Slutmagasinet skiljer sig; jämförelsen nedan värderar skillnaden till LP:ns vattenvärde.")

    lp, rule = summary.loc["lp"], summary.loc["dygnstopp"]
    gain = lp.value - rule.value
    timing = lp.energy * (lp.capture_price - rule.capture_price)
    efficiency = (lp.energy - rule.energy) * rule.capture_price
    print("\nLP-optimeringen jämfört med tumregeln (dygnstopp):")
    print(f"  Merintäkt              {fmt(gain):>8} €  ({gain / rule.value:+.1%})")
    print(f"  varav bättre timing    {fmt(timing):>8} €  (snittpris {lp.capture_price - rule.capture_price:+.1f} €/MWh)")
    print(f"  varav högre verkn.grad {fmt(efficiency):>8} €  ({lp.energy - rule.energy:+.0f} MWh ur samma vatten)")
    even = summary.loc["jamn"]
    print(f"Jämfört med jämn tappning: {fmt(lp.value - even.value)} € ({(lp.value - even.value) / even.value:+.1%})")

    wv = results["lp"].water_value
    slopes = [slope for _, slope in plant.segments()]
    print(f"\nVattenvärde enligt LP: {wv.min():.0f}–{wv.max():.0f} €/MWh. Kraftverket kör när timpriset "
          f"ligger över vattenvärdet,\noch fullt flöde lönar sig först när priset är "
          f"{slopes[0] / slopes[-1] - 1:.0%} högre eftersom verkningsgraden faller över "
          f"{plant.flow_points[1]:g} m³/s.")
    return f"LP-optimering: +{fmt(gain)} € ({gain / rule.value:+.1%}) mot tumregeln på en vecka"


def save_outputs(df: pd.DataFrame, results: dict[str, pd.DataFrame]) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    df.to_csv(OUTPUT_DIR / "indata.csv", float_format="%.3f")
    plan = df.copy()
    for key, r in results.items():
        for col in r.columns:
            plan[f"{key}_{col}"] = r[col].to_numpy()
    plan.to_csv(OUTPUT_DIR / "korplan.csv", float_format="%.3f")
    return OUTPUT_DIR / "korplan.csv"


def run_scenarios(n: int, plant: Plant) -> None:
    """Upprepar jämförelsen över n slumpade veckor för att se hur mycket vinsten varierar."""
    gains = {key: [] for key in RULES}
    for seed in range(1, n + 1):
        df = synthetic_week(seed)
        summary = summarize(run_week(df, plant), df.price.to_numpy(), plant)
        for key in RULES:
            base = summary.loc[key, "value"]
            gains[key].append((summary.loc["lp", "value"] - base, (summary.loc["lp", "value"] - base) / base))

    print(f"\nMerintäkt för LP-optimering över {n} slumpade veckor:")
    print(f"{'jämfört med':<22}{'snitt €/vecka':>14}{'snitt':>8}{'min':>8}{'max':>8}{'≈ per år':>12}")
    for key in RULES:
        euro, pct = np.array(gains[key]).T
        print(f"{LABELS[key]:<22}{fmt(euro.mean()):>14}{pct.mean():>8.1%}{pct.min():>8.1%}{pct.max():>8.1%}"
              f"{fmt(euro.mean() * WEEKS_PER_YEAR / 1e6, 2) + ' M€':>12}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=1, help="slumpfrö för den syntetiska veckan")
    parser.add_argument("--data", help="CSV med timdata (time, price, inflow)")
    parser.add_argument("--scenarios", type=int, default=0, help="kör även N slumpade veckor")
    args = parser.parse_args()

    plant = Plant()
    df = load_csv(args.data) if args.data else synthetic_week(args.seed)
    results = run_week(df, plant)
    summary = summarize(results, df.price.to_numpy(), plant)
    headline = report(df, results, summary, plant)

    plan_path = save_outputs(df, results)
    plot_week(df, results, LABELS, plant, headline, OUTPUT_DIR / "jamforelse.png")
    print(f"\nKörplan: {plan_path}\nFigur:   {OUTPUT_DIR / 'jamforelse.png'}")

    if args.scenarios:
        run_scenarios(args.scenarios, plant)


if __name__ == "__main__":
    main()
