"""Figurer: översikt över backtesten, en detaljvecka, vattenvärden och morgondagens körplan."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from cascade import Cascade

# Kategoriska färger i fast ordning (färgen följer strategin) + neutrala toner.
# Bara den bästa tumregeln ritas, så båda varianterna kan dela tumregelns färg.
COLORS = {"milp": "#2a78d6", "dygnstopp": "#eb6834", "dygnstopp_fullt": "#eb6834", "veckotopp": "#eb6834",
          "jamn": "#eb6834", "vattenvarde": "#eb6834", "styrkurva": "#1baf7a", "perfekt": "#52514e",
          "deterministisk": "#7d5fc4"}
PLANT_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE, SHADE = "#e1e0d9", "#c3c2b7", "#fcfcfb", "#f0efec"
WEEKDAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]
MONTHS = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


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


def _label_end(ax, x, y, text: str, color: str = INK) -> None:
    ax.annotate(text, (x, y), xytext=(6, 0), textcoords="offset points", va="center", fontsize=9, color=color)


def _label_ends(ax, x, labels: list[tuple[float, str, str]]) -> None:
    """Etiketter (y, text, färg) vid linjernas slut, nedifrån och upp med minsta avstånd så att de
    inte hamnar på varandra."""
    low, high = ax.get_ylim()
    previous = -np.inf
    for y, text, color in sorted(labels):
        previous = max(y, previous + 0.05 * (high - low))
        _label_end(ax, x, previous, text, color)


def _finish(fig, headline: str, path: Path, legend_handles=None, legend_labels=None) -> None:
    if legend_handles:
        fig.legend(legend_handles, legend_labels, loc="upper center", ncol=len(legend_labels),
                   frameon=False, bbox_to_anchor=(0.5, 0.965), labelcolor=INK)
    fig.suptitle(headline, x=0.06, y=0.995, ha="left", fontsize=14, color=INK, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 0.94, 0.95))
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def _day_axis(ax) -> None:
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(plt.FuncFormatter(
        lambda x, _: (d := mdates.num2date(x)) and f"{WEEKDAYS[d.weekday()]} {d.day}/{d.month}"))


def _month_axis(ax) -> None:
    def month_label(x, _):
        d = mdates.num2date(x)
        return f"{MONTHS[d.month - 1]}\n{d.year}" if d.month == 1 else MONTHS[d.month - 1]

    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(plt.FuncFormatter(month_label))


def backtest_overview(cumulative: dict[str, pd.Series], volumes: dict[str, pd.Series], guide: pd.Series,
                      price: pd.Series, labels: dict[str, str], headline: str, path: Path) -> None:
    """Ackumulerat värde mot tumregeln (intäkt minus startkostnader plus förändringen i vattnets
    värde), Trängslets magasin över perioden och spotpriset."""
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True, gridspec_kw={"height_ratios": [1.3, 1, 0.8]})
    fig.patch.set_facecolor(SURFACE)

    ax = axes[0]
    for key, series in cumulative.items():
        ax.plot(series.index, series / 1000, color=COLORS[key], lw=1.8, ls="--" if key == "perfekt" else "-",
                label=labels[key])
    ax.axhline(0, color=AXIS, lw=0.8)
    end = series.index[-1]
    _label_end(ax, end, 0, "tumregel", MUTED)
    _label_ends(ax, end, [(s.iloc[-1] / 1000, f"{s.iloc[-1] / 1000:+,.0f} k€".replace(",", " "), COLORS[k])
                          for k, s in cumulative.items()])
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK_2)
    _style(ax, "Värde mot tumregeln (k€)\ninkl. vattnets värde")

    ax = axes[1]
    ax.plot(guide.index, guide, color=MUTED, lw=1.0, label="Styrkurva")
    for key, series in volumes.items():
        ax.plot(series.index, series, color=COLORS[key], lw=1.5, label=labels[key])
    ax.set_ylim(0, 1.3 * max(s.max() for s in volumes.values()))   # plats för förklaringen ovanför kurvorna
    ax.legend(loc="upper center", frameon=False, fontsize=9, labelcolor=INK_2, ncol=4)
    _style(ax, "Trängslets magasin (Mm³)")

    ax = axes[2]
    ax.plot(price.index, price, color=INK_2, lw=1.2)
    _style(ax, "Spotpris SE3,\ndygnsmedel (€/MWh)")
    _month_axis(ax)
    ax.set_xlim(price.index[0], price.index[-1])
    _finish(fig, headline, path)


def water_values(wv, cascade: Cascade, headline: str, path: Path) -> None:
    """Vattenvärdet över året vid olika fyllnadsgrad, och styrkurvan med spridning."""
    upper = cascade.plants[0]
    energy = cascade.downstream_energy()[0]
    weeks = pd.date_range("2026-01-04", periods=52, freq="7D")  # mitt i varje vecka, år spelar ingen roll
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    fig.patch.set_facecolor(SURFACE)

    ax = axes[0]
    ramp = {0.25: "#86b6ef", 0.5: "#2a78d6", 0.75: "#104281"}  # ordnade nivåer: ljus till mörk
    for fill, color in ramp.items():
        segment = min(int(fill * upper.v_max // np.diff(wv.volumes)[0]), len(wv.volumes) - 2)
        value = [wv.marginal(t)[segment] / energy for t in weeks]
        ax.plot(weeks, value, color=color, lw=1.8, label=f"{fill:.0%} fyllnad")
        _label_end(ax, weeks[-1], value[-1], f"{fill:.0%}", INK_2)
    ax.legend(loc="upper center", frameon=False, fontsize=9, labelcolor=INK_2, ncol=3)
    _style(ax, "Vattenvärde (€/MWh)")

    ax = axes[1]
    pct = 100 * np.array([[wv.guide[k, i] for k in range(52)] for i in range(3)]) / upper.v_max
    ax.fill_between(weeks, pct[0], pct[2], color="#cde2fb", lw=0, label="10–90 % av utfallen")
    ax.plot(weeks, pct[1], color="#2a78d6", lw=1.8, label="Styrkurva (median)")
    ax.set_ylim(0, 100)
    ax.legend(loc="lower right", frameon=False, fontsize=9, labelcolor=INK_2)
    _style(ax, "Fyllnadsgrad (%)")
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: MONTHS[mdates.num2date(x).month - 1]))
    ax.set_xlim(pd.Timestamp("2026-01-01"), pd.Timestamp("2026-12-31"))
    _finish(fig, headline, path)


def detail_week(time: pd.DatetimeIndex, price: np.ndarray, outcomes: dict, cascade: Cascade,
                labels: dict[str, str], headline: str, path: Path) -> None:
    """En vecka per kvart: pris, produktion per kraftverk och Åsens damm."""
    n = len(cascade.plants)
    fig, axes = plt.subplots(n + 2, 1, figsize=(12, 12), sharex=True)
    fig.patch.set_facecolor(SURFACE)

    axes[0].step(time, price, where="post", color=INK_2, lw=1.0)
    _style(axes[0], "Spotpris (€/MWh)")
    for p, plant in enumerate(cascade.plants):
        ax = axes[p + 1]
        for i, (key, outcome) in enumerate(outcomes.items()):
            ax.step(time, outcome.power[:, p], where="post", color=COLORS[key], lw=1.6 if i else 1.1,
                    label=labels[key])
        _style(ax, f"{plant.name}\n(MW)")
    ax = axes[-1]
    pond = 1
    for i, (key, outcome) in enumerate(outcomes.items()):
        ax.plot(time, outcome.volume[:, pond], color=COLORS[key], lw=1.6 if i else 1.1)
    ax.axhline(cascade.plants[pond].v_max, color=MUTED, lw=0.8)
    ax.axhline(0, color=MUTED, lw=0.8)
    _style(ax, f"Damm vid\n{cascade.plants[pond].name} (Mm³)")
    _day_axis(ax)
    ax.set_xlim(time[0], time[-1])
    handles, names = axes[1].get_legend_handles_labels()
    _finish(fig, headline, path, handles, names)


def actual_comparison(time: pd.DatetimeIndex, price: np.ndarray, week: dict[str, np.ndarray],
                      monthly: pd.DataFrame, colors: dict[str, str], headline: str, path: Path) -> None:
    """En vecka med verklig och modellerad produktion, och snittpriset per månad för var och en."""
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), gridspec_kw={"height_ratios": [0.8, 1.2, 1]})
    fig.patch.set_facecolor(SURFACE)

    ax = axes[0]
    ax.step(time, price, where="post", color=INK_2, lw=1.0)
    _style(ax, "Spotpris (€/MWh)")
    ax.sharex(axes[1])
    ax.tick_params(labelbottom=False)

    ax = axes[1]
    for label, power in week.items():
        ax.step(time, power, where="post", color=colors[label], lw=1.4, label=label)
    ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=INK_2)
    _style(ax, "Produktion (MW)")
    _day_axis(ax)
    ax.set_xlim(time[0], time[-1])

    ax = axes[2]
    for label in monthly.columns:
        ax.plot(monthly.index, monthly[label], color=colors[label], lw=1.8 if label in week else 1.2,
                marker="o", ms=3, label=label)
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK_2, ncol=2)
    _style(ax, "Snittpris för\nproduktionen (€/MWh)")
    _month_axis(ax)
    _finish(fig, headline, path)


def regulated_flow(flows: pd.DataFrame, colors: dict[str, str], headline: str, path: Path) -> None:
    """Flöde per dygn vid sista kraftverket, verkligt och i modellen, och den ackumulerade
    tappningen sedan periodens början."""
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [1.2, 1]})
    fig.patch.set_facecolor(SURFACE)
    cumulative = flows.cumsum() * 86400 / 1e6
    for label in flows.columns:
        width = 2.0 if label == flows.columns[0] else 1.3
        axes[0].plot(flows.index, flows[label], color=colors[label], lw=width, label=label)
        axes[1].plot(cumulative.index, cumulative[label], color=colors[label], lw=width)
    _style(axes[0], "Flöde per dygn (m³/s)")
    _style(axes[1], "Tappat sedan start\n(Mm³)")
    _month_axis(axes[1])
    axes[1].set_xlim(flows.index[0], flows.index[-1])
    _label_ends(axes[1], cumulative.index[-1], [(cumulative[label].iloc[-1], f"{cumulative[label].iloc[-1]:.0f}",
                                                 colors[label]) for label in flows.columns])
    handles, names = axes[0].get_legend_handles_labels()
    _finish(fig, headline, path, handles, names)


def day_plan(time: pd.DatetimeIndex, price: np.ndarray, n_known: int, power: np.ndarray,
             fill: np.ndarray, cascade: Cascade, headline: str, path: Path) -> None:
    """Körplan för horisonten. Det första dygnet är det som läggs som bud; resten är preliminärt."""
    fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True, gridspec_kw={"height_ratios": [1, 1.3, 1]})
    fig.patch.set_facecolor(SURFACE)
    for ax in axes:
        ax.axvspan(time[96], time[-1], color=SHADE, lw=0, zorder=0)

    ax = axes[0]
    ax.step(time[:n_known + 1], price[:n_known + 1], where="post", color=INK, lw=1.4, label="Publicerat spotpris")
    ax.step(time[n_known:], price[n_known:], where="post", color=MUTED, lw=1.0, ls="--", label="Prisprognos")
    ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=INK_2)
    _style(ax, "Spotpris (€/MWh)")

    ax = axes[1]
    areas = ax.stackplot(time, power.T, colors=PLANT_COLORS, step="post", edgecolor=SURFACE, linewidth=0.3)
    _style(ax, "Produktion (MW)")

    ax = axes[2]
    for p in range(len(cascade.plants)):
        ax.plot(time, 100 * fill[:, p], color=PLANT_COLORS[p], lw=1.6)
    ax.set_ylim(0, 100)
    _style(ax, "Fyllnadsgrad (%)")
    _day_axis(ax)
    ax.set_xlim(time[0], time[-1])
    _finish(fig, headline, path, areas, [p.name for p in cascade.plants])
