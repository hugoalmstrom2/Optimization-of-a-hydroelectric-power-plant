"""Indata till veckoplaneringen: timpriser (€/MWh) och tillrinning (m³/s).

Standardscenariot är syntetiskt men byggt för att likna en höstvecka i SE2:
dygnsprofil med morgon- och kvällstopp, billigare helg, autokorrelerade
prisavvikelser (t.ex. vindprognosfel) och en regnfront som ger en
tillrinningstopp någon gång i veckan. Riktiga data läses in med load_csv().
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HOURS_PER_WEEK = 168


def synthetic_week(seed: int = 1, start: str = "2026-10-19") -> pd.DataFrame:
    """Slumpar en vecka med start måndag 00:00. Samma seed ger samma vecka."""
    rng = np.random.default_rng(seed)
    time = pd.date_range(start, periods=HOURS_PER_WEEK, freq="h")
    hour = time.hour.to_numpy()
    weekday = time.dayofweek.to_numpy()

    # Dygnsprofil: morgontopp ~08, kvällstopp ~18-19, nattdal ~03.
    profile = (1.0
               + 0.30 * np.exp(-((hour - 8.0) / 1.8) ** 2)
               + 0.40 * np.exp(-((hour - 18.5) / 2.2) ** 2)
               - 0.35 * np.exp(-((hour - 3.0) / 2.5) ** 2))
    # Helgen har plattare profil (mindre industrilast).
    profile = np.where(weekday >= 5, 1.0 + 0.5 * (profile - 1.0), profile)

    # Dagsnivå i €/MWh: vardagar dyrare än helg, slumpmässig variation per dag.
    base_level = np.array([58, 64, 68, 60, 52, 38, 32], dtype=float)
    day_level = base_level * rng.uniform(0.7, 1.3, size=7)

    # Autokorrelerat brus: avvikelser håller i sig några timmar.
    noise = np.zeros(HOURS_PER_WEEK)
    for t in range(1, HOURS_PER_WEEK):
        noise[t] = 0.85 * noise[t - 1] + rng.normal(0.0, 4.0)

    price = day_level[weekday] * profile + noise

    # Tillrinning: basflöde + regnfront med topp ett dygn efter regnstart och
    # långsam avklingning (x * e^(1-x), x = dygn sedan regnstart).
    base_inflow = rng.uniform(80, 110)
    rain_start = rng.uniform(12, 96)
    rain_size = rng.uniform(30, 70)
    x = np.clip(np.arange(HOURS_PER_WEEK) - rain_start, 0, None) / 24
    inflow = base_inflow + rain_size * x * np.exp(1 - x)

    return pd.DataFrame({"price": price, "inflow": inflow}, index=pd.Index(time, name="time"))


def load_csv(path: str) -> pd.DataFrame:
    """Läser timdata med kolumnerna time, price (€/MWh) och inflow (m³/s)."""
    df = pd.read_csv(path, parse_dates=["time"]).set_index("time")
    missing = {"price", "inflow"} - set(df.columns)
    if missing:
        raise ValueError(f"{path} saknar kolumnerna {sorted(missing)}")
    if len(df) % 24:
        raise ValueError(f"{path} har {len(df)} timmar, förväntade hela dygn (multipel av 24)")
    return df[["price", "inflow"]]
