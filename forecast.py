"""Prognoser som planeringen har tillgång till vid beslutstillfället.

Pris: perioder som redan är prissatta på day-ahead-marknaden används som de är. Övriga
prognostiseras som snittet för samma tid på dygnet de senaste sju dygnen, plus den senaste
kända periodens avvikelse från den profilen, som klingar av med 10 % per timme. Det ger
dygnets form. Nivån, alltså dygnets medelpris, kommer från en linjär regression på samma tid
i går och förra veckan, veckoprofilen, gårdagens medelpris och kväll samt veckodag, skattad
på de senaste åtta veckorna. Nivåkorrigeringen halveras för varje dygn längre fram.

Över oktober 2025 – oktober 2026 i SE3 gav det ett medelfel på 23,0 €/MWh för nästa dygn
(25,3 utan nivåmodellen) och 31–34 €/MWh för dygn 2–7. "Samma kvart förra veckan" ger
ungefär 37. En riktig prisprognos med väder och vind bör göra det bättre, så resultaten här
är snarare i underkant.

Prisscenarier för budgivningen: prognosen plus verkliga prognosfel för nästa dygn från de
senaste fyra veckorna, valda vid jämnt fördelade kvantiler av dygnets medelfel, från det
minsta till det största. Budkurvan ska täcka hela det prisintervall som är troligt.

Tillrinning: säsongsnormalen plus senaste observerade avvikelse från den, som klingar av med
2 % per dygn. Normalen är medlet för samma dag på året (utjämnat över 15 dygn) från åren före
prognosen. Vårflodens början syns då i prognosen, medan ett ovanligt vått eller torrt läge
består. För Trängslet 2013–2025 gav det ett medelfel på 8,4 m³/s över 1–7 dygn, mot 9,4 för
persistens (att senaste dygnsmedel består); avklingningen valdes på 2000–2012.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WEEK = pd.Timedelta(days=7)
DAY = pd.Timedelta(days=1)
LEVEL_DECAY = 0.9          # andel av senaste avvikelsen från profilen som finns kvar en timme senare
LEVEL_TRAIN_DAYS = 56      # dygn som nivåmodellen skattas på
LEVEL_PERSISTENCE = 0.5    # andel av nivåkorrigeringen som finns kvar dygnet efter
INFLOW_PERSISTENCE = 0.98  # andel av tillrinningens avvikelse från normalen som finns kvar dygnet efter
NORMAL_SMOOTHING = 15      # dygn, glidande medel av säsongsnormalen


def _step(series: pd.Series) -> pd.Timedelta:
    return series.index[1] - series.index[0]


def _level_features(days: np.ndarray, j: int, weekday: int) -> np.ndarray:
    """Förklarande variabler per period för dygn j i matrisen days (dygn, perioder på dygnet)."""
    per_day = days.shape[1]
    yesterday = days[j - 1]
    week_day = np.zeros((per_day, 6))
    if weekday < 6:
        week_day[:, weekday] = 1
    return np.column_stack([np.ones(per_day), yesterday, days[j - 7], days[j - 7:j].mean(axis=0),
                            np.full(per_day, yesterday.mean()), np.full(per_day, yesterday[-per_day // 12:].mean()),
                            week_day])


def _level(known: pd.Series, day: pd.Timestamp) -> float | None:
    """Prognos för dygnets medelpris, eller None om historiken inte räcker."""
    step = _step(known)
    per_day = int(DAY / step)
    n = LEVEL_TRAIN_DAYS + 7
    days = known.reindex(pd.date_range(day - n * DAY, periods=n * per_day, freq=step)).to_numpy().reshape(n, per_day)
    weekday = lambda j: (day - (n - j) * DAY).weekday()  # noqa: E731
    X = np.vstack([_level_features(days, j, weekday(j)) for j in range(7, n)])
    y = days[7:].ravel()
    ok = ~np.isnan(X).any(axis=1) & ~np.isnan(y)
    x = _level_features(days, n, day.weekday())
    if ok.sum() < 14 * per_day or np.isnan(x).any():
        return None
    ridge = 1e-3 * ok.sum() * np.eye(X.shape[1])
    ridge[0, 0] = 0
    beta = np.linalg.solve(X[ok].T @ X[ok] + ridge, X[ok].T @ y[ok])
    return float((x @ beta).mean())


def price_path(known: pd.Series, start: pd.Timestamp, steps: int) -> tuple[np.ndarray, int]:
    """Priser för horisonten i samma tidsupplösning som known: kända priser där de finns,
    annars prognos. Returnerar (priser, antal kända perioder i början av horisonten)."""
    step = _step(known)
    index = pd.date_range(start, periods=steps, freq=step)
    actual = known.reindex(index)
    n_known = int(actual.notna().cumprod().sum())

    last_week = known[known.index > known.index[-1] - WEEK]
    slot = lambda idx: (idx - idx.normalize()) // step  # noqa: E731  period på dygnet
    profile = last_week.groupby(slot(last_week.index)).mean()
    deviation = known.iloc[-1] - profile[slot(known.index[-1:])[0]]
    hours_ahead = (index[n_known:] - known.index[-1]) / pd.Timedelta(hours=1)

    path = actual.to_numpy()
    path[n_known:] = profile.reindex(slot(index[n_known:])).to_numpy() + deviation * LEVEL_DECAY ** hours_ahead
    first = index[n_known] if n_known < steps else None
    if first is not None and first == first.normalize():
        level = _level(known[known.index < first], first)
        if level is not None:
            per_day = int(DAY / step)
            days_ahead = np.arange(steps - n_known) // per_day
            path[n_known:] += (level - path[n_known:n_known + per_day].mean()) * LEVEL_PERSISTENCE ** days_ahead
    return path, n_known


def price_scenarios(known: pd.Series, day: pd.Timestamp, steps: int, n: int,
                    lookback_days: int = 28) -> np.ndarray:
    """n prisscenarier (n, steps) för en horisont som börjar med ett dygn utan kända priser.
    Första dygnet får prognosen plus prognosfelet från ett av de senaste lookback_days
    dygnen; resten av horisonten är prognosen."""
    base, _ = price_path(known, day, steps)
    per_day = int(DAY / _step(known))
    errors = []
    for k in range(1, lookback_days + 1):
        past = day - k * DAY
        forecast, _ = price_path(known[known.index < past], past, per_day)
        actual = known.reindex(pd.date_range(past, periods=per_day, freq=_step(known))).to_numpy()
        if not np.isnan(actual).any():
            errors.append(actual - forecast)
    errors = np.array(errors)
    order = np.argsort(errors.mean(axis=1))
    picks = order[np.round(np.linspace(0, 1, n) * (len(order) - 1)).astype(int)]
    scenarios = np.tile(base, (n, 1))
    scenarios[:, :per_day] += errors[picks]
    return scenarios


def scenario_weights(n: int) -> np.ndarray:
    """Sannolikheter för scenarier vid jämnt fördelade kvantiler från minsta till största felet:
    varje scenario står för halva avståndet till grannarna, så ändpunkterna väger hälften."""
    weights = np.ones(n)
    weights[[0, -1]] = 0.5
    return weights / weights.sum()


def _day_of_year(index: pd.DatetimeIndex) -> np.ndarray:
    """Dag 1–365; skottdagen räknas som 28 februari."""
    shift = (index.is_leap_year & (index.month > 2)).astype(int)
    return np.minimum(index.dayofyear - shift, 365)


def inflow_normal(daily: pd.DataFrame, before: pd.Timestamp) -> pd.DataFrame:
    """Säsongsnormal för tillrinningen (m³/s) per dag på året (1–365) och kraftverk, från dygnen
    före before, utjämnad med ett glidande medel som går runt årsskiftet."""
    past = daily[daily.index < before]
    normal = past.groupby(_day_of_year(past.index)).mean().reindex(range(1, 366)).interpolate()
    pad = NORMAL_SMOOTHING // 2
    wrapped = pd.concat([normal.iloc[-pad:], normal, normal.iloc[:pad]])
    smooth = wrapped.rolling(NORMAL_SMOOTHING, center=True).mean().iloc[pad:-pad]
    smooth.index = normal.index
    return smooth


def inflow_path(observed: pd.DataFrame, last_observed: pd.Timestamp, start: pd.Timestamp,
                steps: int, normal: pd.DataFrame | None = None) -> np.ndarray:
    """Lokal tillrinning (perioder, kraftverk). Utan normal: senaste observerade dygnsmedel
    framåt (persistens). Med normal (från inflow_normal): normalen plus senaste avvikelsen från
    den, som klingar av med INFLOW_PERSISTENCE per dygn."""
    noon = last_observed.normalize() + pd.Timedelta(hours=12)
    level = observed.loc[noon].to_numpy()
    if normal is None:
        return np.tile(level, (steps, 1))
    times = pd.date_range(start, periods=steps, freq=_step(observed))
    anomaly = level - normal.loc[_day_of_year(pd.DatetimeIndex([noon]))[0]].to_numpy()
    days_ahead = ((times - noon) / DAY).to_numpy()
    path = normal.loc[_day_of_year(times)].to_numpy() + anomaly * INFLOW_PERSISTENCE ** days_ahead[:, None]
    return np.clip(path, 0, None)
