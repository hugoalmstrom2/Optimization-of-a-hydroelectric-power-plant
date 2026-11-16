"""Figur som jämför strategierna timme för timme över veckan."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from model import Plant

# Kategoriska färger i fast ordning (färgen följer strategin) + neutrala toner.
COLORS = {"lp": "#2a78d6", "dygnstopp": "#eb6834", "jamn": "#1baf7a"}
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE, WEEKEND = "#e1e0d9", "#c3c2b7", "#fcfcfb", "#f0efec"
WEEKDAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]
DRAW_ORDER = ["jamn", "dygnstopp", "lp"]  # LP ritas sist så att den hamnar överst


def _style(ax, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_ylabel(ylabel, color=INK_2)
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelcolor=INK_2)


def _hline(ax, y: float, text: str) -> None:
    ax.axhline(y, color=MUTED, lw=0.8)
    ax.annotate(text, (1.0, y), xycoords=("axes fraction", "data"), xytext=(4, 0),
                textcoords="offset points", va="center", fontsize=8, color=MUTED)


def plot_week(df: pd.DataFrame, results: dict[str, pd.DataFrame], labels: dict[str, str],
              plant: Plant, headline: str, path: Path) -> None:
    t = df.index
    fig, axes = plt.subplots(4, 1, figsize=(12, 11), sharex=True,
                             gridspec_kw={"height_ratios": [1, 1.2, 1, 1]})
    fig.patch.set_facecolor(SURFACE)

    for ax in axes:
        for day in t[(t.dayofweek == 5) & (t.hour == 0)]:
            ax.axvspan(day, min(day + pd.Timedelta(days=2), t[-1]), color=WEEKEND, lw=0, zorder=0)

    ax = axes[0]
    ax.step(t, df.price, where="post", color=INK_2, lw=1.4, label="Elpris")
    ax.step(t, results["lp"].water_value, where="post", color=COLORS["lp"], lw=1.4, ls="--",
            label="Vattenvärde enligt LP (kör när priset ligger över)")
    ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=INK_2)
    _style(ax, "Elpris (€/MWh)")

    ax = axes[1]
    for key in DRAW_ORDER:
        ax.step(t, results[key].power, where="post", color=COLORS[key],
                lw=1.8 if key == "lp" else 1.3, label=labels[key])
    _hline(ax, plant.power_points[1], "bästa\nverkningsgrad")
    _style(ax, "Produktion (MW)")

    ax = axes[2]
    for key in DRAW_ORDER:
        ax.plot(t, results[key].volume, color=COLORS[key], lw=1.8 if key == "lp" else 1.3)
    _hline(ax, plant.v_max, "dämningsgräns")
    _hline(ax, plant.v_min, "sänkningsgräns")
    _style(ax, "Magasin (Mm³)")

    ax = axes[3]
    for key in DRAW_ORDER:
        cumulative = (df.price.to_numpy() * results[key].power).cumsum() / 1000
        ax.plot(t, cumulative, color=COLORS[key], lw=1.8 if key == "lp" else 1.3)
        ax.annotate(f"{cumulative.iloc[-1]:,.0f} k€".replace(",", " "), (t[-1], cumulative.iloc[-1]),
                    xytext=(6, 0), textcoords="offset points", va="center", fontsize=9, color=INK)
    _style(ax, "Ackumulerad intäkt (k€)")

    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(plt.FuncFormatter(
        lambda x, _: (d := mdates.num2date(x)) and f"{WEEKDAYS[d.weekday()]} {d.day}/{d.month}"))
    ax.set_xlim(t[0], t[-1])

    handles, names = axes[1].get_legend_handles_labels()
    order = [names.index(labels[k]) for k in ("lp", "dygnstopp", "jamn")]
    fig.legend([handles[i] for i in order], [names[i] for i in order], loc="upper center",
               ncol=3, frameon=False, bbox_to_anchor=(0.5, 0.97), labelcolor=INK)
    fig.suptitle(headline, x=0.07, y=0.995, ha="left", fontsize=14, color=INK, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 0.96, 0.955))
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
