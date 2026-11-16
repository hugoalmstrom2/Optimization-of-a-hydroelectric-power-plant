"""Vattenvärden för det övre magasinet med stokastisk dynamisk programmering (SDP).

Långtidsplaneringen avgör vad vattnet i magasinet är värt, beroende på årstid och
fyllnadsgrad. Korttidsplaneringen använder det som värde på vattnet vid horisontens
slut. Tumreglerna följer den typiska fyllnadsbana som den optimala policyn ger
(styrkurvan).

Ett steg per vecka, cykliskt över året:

    F_k(V) = E[ max_V' { intäkt_k(X, V, V') - straff(X < Xmin) + β · F_k+1(V') } ],   X = V + I − V'

V är volymen när vecka k börjar, I är veckans tillrinning och X det som släpps. Intäkten
räknas med veckans varaktighetskurva för priset: vatten upp till kaskadens flaskhals
(kraftverket nedströms med minst slukförmåga) ger effekt i alla kraftverk under veckans
dyraste timmar; vatten därutöver ger bara effekt i det översta kraftverket (resten spills
nedströms), och det som överstiger dess kapacitet spills. Under de månader då kraftverken
nedströms inte får korttidsreglera (1 juni–31 augusti) får deras del av effekten veckans
medelpris i stället, eftersom de då går jämnt. Fallhöjden, och därmed effekten,
beror på magasinsnivån. Xmin är den tappning som krävs för att minimitappningen nedströms
ska klaras med den lokala tillrinningen; utan det ser modellen inte att sista vattnet i
magasinet behövs för vattendomen under senvintern. Beslutet fattas när veckans tillrinning
och priser är kända.

Scenarierna är historiska år sedan elområdesindelningen 2011 där både spotpriser för SE3 och
tillrinning (SMHI Idre 3) finns. Pris och tillrinning från samma år hör ihop, så sambandet
mellan våta år och låga priser följer med. Varje år ger fem prisscenarier per vecka: samma
vecka och de två närmaste på vardera sidan. Prisnivån har ändrats mycket sedan 2012, så alla
år skalas med samma faktor till det senaste årets nivå; skillnaderna mellan åren behålls.
Bara data från före `until` används, så en backtest ser aldrig framtiden.

Vattenvärdena kan också räknas riskavert. Då ersätts väntevärdet i varje steg av en blandning
av väntevärdet och medelvärdet av de sämsta utfallen (CVaR). Framtida intäkter är osäkrare än
dagens, så riskaversion mot intäkten gör att vattnet säljs tidigare och magasinet hålls lägre.
Det som skyddar mot ett tomt magasin är vattendomen.
"""
from __future__ import annotations

import hashlib
import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from cascade import MM3_PER_M3S_HOUR, Cascade
from data import CACHE, HISTORY_START, daily_inflows, load_prices

WEEKS = 52
HOURS_PER_WEEK = 168
GRID_STEP = 20.0                    # Mm³ mellan volymnivåerna
DISCOUNT = 0.95 ** (1 / WEEKS)      # 5 % ränta per år
CYCLES = 5                          # varv bakåt genom året tills vattenvärdena stabiliserats
PRICE_WINDOW = 2                    # veckor på vardera sidan som också används som prisscenarier
LEVEL_DAYS = 365                    # prisnivån skalas till medelpriset det senaste året
GUIDE_SMOOTHING = 3                 # veckor, glidande medel av styrkurvan
MIN_RELEASE_PENALTY = 1e6           # € per Mm³ som saknas för vattendomen
RISK_NEUTRAL = (0.0, 0.2)
RISK_AVERSE = (0.5, 0.2)            # vikt på CVaR och andel sämsta utfall som den räknas på


def risk_adjusted(samples: np.ndarray, weights: np.ndarray, weight: float, tail: float) -> np.ndarray:
    """(1 - weight) · väntevärde + weight · CVaR, alltså medelvärdet av de sämsta utfallen som
    tillsammans har sannolikheten tail. samples är (utfall, volymnivåer), weights sannolikheterna."""
    mean = weights @ samples
    if weight == 0:
        return mean
    order = np.argsort(samples, axis=0)
    ordered = np.take_along_axis(samples, order, axis=0)
    w = weights[order]
    before = np.cumsum(w, axis=0) - w
    in_tail = np.clip(np.minimum(w, tail - before), 0, None)
    return (1 - weight) * mean + weight * (in_tail * ordered).sum(axis=0) / tail


@dataclass
class WaterValues:
    volumes: np.ndarray             # volymnivåer (Mm³)
    values: np.ndarray              # (vecka, nivå): förväntat framtida värde (€) när veckan börjar
    guide: np.ndarray               # (vecka, 3): 10-, 50- och 90-percentil för volymen enligt policyn
    years: tuple[int, int]          # scenarioåren
    price_level: float              # €/MWh som priserna skalats till

    @staticmethod
    def _position(when: pd.Timestamp) -> tuple[int, int, float]:
        x = min((when.dayofyear - 1 + when.hour / 24) / 7, WEEKS - 1e-9)
        k = int(x)
        return k, (k + 1) % WEEKS, x - k

    def value_function(self, when: pd.Timestamp) -> np.ndarray:
        k, k_next, frac = self._position(when)
        return (1 - frac) * self.values[k] + frac * self.values[k_next]

    def marginal(self, when: pd.Timestamp) -> np.ndarray:
        """Vattenvärde (€/Mm³) per volymsegment. Icke-växande och icke-negativt, dvs konkavt."""
        slopes = np.diff(self.value_function(when)) / np.diff(self.volumes)
        return np.clip(np.minimum.accumulate(slopes), 0, None)

    def target(self, when: pd.Timestamp) -> float:
        k, k_next, frac = self._position(when)
        return float((1 - frac) * self.guide[k, 1] + frac * self.guide[k_next, 1])

    def level(self, when: pd.Timestamp, energy_equivalent: float) -> float:
        """Vattenvärdet vid styrkurvan uttryckt i €/MWh."""
        segment = min(int(self.target(when) // np.diff(self.volumes)[0]), len(self.volumes) - 2)
        return float(self.marginal(when)[segment] / energy_equivalent)


@dataclass
class MonthlyWaterValues:
    """Vattenvärden som räknas om vid varje månadsskifte med den data som fanns då,
    som i drift där långtidsplaneringen uppdateras löpande."""
    by_month: dict[pd.Timestamp, WaterValues]

    def at(self, when: pd.Timestamp) -> WaterValues:
        """Vattenvärdena som var kända vid tidpunkten when."""
        return self.by_month[when.normalize().replace(day=1)]


def solve_sdp(volumes: np.ndarray, inflow: np.ndarray, cum_prices: list[list[list[np.ndarray]]],
              hourly_release: float, full_power, extra_release: float = 0.0, extra_power=0.0,
              min_release: np.ndarray | None = None,
              risk: tuple[float, float] = RISK_NEUTRAL, flat_power: float = 0.0,
              flat_weeks: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Själva SDP:n.

    inflow och min_release är (år, vecka) i Mm³ per vecka. cum_prices[k][y] är prisscenarierna
    för vecka k i år y: kumulerade summor av timpriserna sorterade från dyrast (169 värden,
    första är 0). Tillrinning och priser för samma år hör ihop. hourly_release (Mm³/h) är
    flödet genom hela kaskaden när den går för fullt och full_power (MW) effekten då;
    extra_release och extra_power gäller vatten därutöver som bara går genom översta
    kraftverket. Effekterna kan vara matriser (startnivå, slutnivå) för att spegla fallhöjden.
    risk är (vikt, andel) för CVaR, se risk_adjusted; (0, ·) ger väntevärdet. flat_power är den
    del av full_power som under veckorna i flat_weeks bara får veckans medelpris.
    Returnerar (värde per vecka och volymnivå, styrkurva per vecka)."""
    base_max = hourly_release * HOURS_PER_WEEK
    extra_max = extra_release * HOURS_PER_WEEK
    hours = np.arange(HOURS_PER_WEEK + 1)
    min_release = np.zeros_like(inflow) if min_release is None else min_release
    flat_weeks = np.zeros(WEEKS, dtype=bool) if flat_weeks is None else flat_weeks
    n_years = len(inflow)

    def revenue(release: np.ndarray, cum: np.ndarray, flat: bool) -> np.ndarray:
        base = np.clip(release, 0, base_max)
        if flat:
            value = ((full_power - flat_power) * np.interp(base / hourly_release, hours, cum)
                     + flat_power * base / hourly_release * cum[-1] / HOURS_PER_WEEK)
        else:
            value = full_power * np.interp(base / hourly_release, hours, cum)
        if extra_release:
            extra = np.clip(release - base_max, 0, extra_max)
            value = value + extra_power * np.interp(extra / extra_release, hours, cum)
        return value

    def best(future: np.ndarray, week_inflow: float, week_min: float, cum: np.ndarray, flat: bool = False):
        """Bästa beslut för varje startvolym: värde och index för ny volym."""
        release = volumes[:, None] + week_inflow - volumes[None, :]
        value = (revenue(release, cum, flat) + DISCOUNT * future[None, :]
                 - MIN_RELEASE_PENALTY * np.clip(week_min - release, 0, None))
        value[release < -1e-9] = -np.inf     # kan inte lagra mer än det som rinner till
        return value.max(axis=1), value.argmax(axis=1)

    values = np.zeros((WEEKS, len(volumes)))
    future = np.zeros(len(volumes))
    for _ in range(CYCLES):
        for k in reversed(range(WEEKS)):
            # Alla år lika sannolika, och prisscenarierna lika sannolika inom året.
            samples = np.array([best(future, inflow[y, k], min_release[y, k], cum, flat_weeks[k])[0]
                                for y in range(n_years) for cum in cum_prices[k][y]])
            weights = np.concatenate([np.full(len(cum_prices[k][y]), 1 / (n_years * len(cum_prices[k][y])))
                                      for y in range(n_years)])
            values[k] = risk_adjusted(samples, weights, *risk)
            future = values[k]

    # Styrkurvan: simulera policyn genom varje år och prisscenario, två varv.
    n_windows = max(len(c) for week in cum_prices for c in week)
    level = np.full((n_years, n_windows), len(volumes) // 2)
    path = np.zeros((WEEKS, n_years, n_windows))
    for _ in range(2):
        for k in range(WEEKS):
            path[k] = volumes[level]
            for y in range(n_years):
                for j in range(n_windows):
                    cum = cum_prices[k][y][j % len(cum_prices[k][y])]
                    _, choice = best(values[(k + 1) % WEEKS], inflow[y, k], min_release[y, k], cum, flat_weeks[k])
                    level[y, j] = choice[level[y, j]]
    guide = np.percentile(path.reshape(WEEKS, -1), [10, 50, 90], axis=1).T
    kernel = np.ones(GUIDE_SMOOTHING) / GUIDE_SMOOTHING
    pad = GUIDE_SMOOTHING // 2
    guide = np.array([np.convolve(np.concatenate([g[-pad:], g, g[:pad]]), kernel, mode="valid")
                      for g in guide.T]).T
    return values, guide


def _week_start(year: int, k: int) -> pd.Timestamp:
    return pd.Timestamp(year=year, month=1, day=1) + pd.Timedelta(days=7 * k)


def _price_scenarios(prices: pd.Series, years: list[int], window: int = PRICE_WINDOW) -> list[list[list[np.ndarray]]]:
    """[vecka][år]: kumulerade summor av timpriserna sorterade från dyrast, för samma vecka och
    de window närmaste veckorna på vardera sidan inom samma år."""
    weekly = {}
    for y in years:
        for k in range(WEEKS):
            week = prices[_week_start(y, k):_week_start(y, k) + pd.Timedelta(hours=HOURS_PER_WEEK - 1)].to_numpy()
            if len(week) == HOURS_PER_WEEK and not np.isnan(week).any():
                weekly[y, k] = np.concatenate([[0], np.cumsum(np.sort(week)[::-1])])
    return [[[weekly[y, (k + d) % WEEKS] for d in range(-window, window + 1) if (y, (k + d) % WEEKS) in weekly]
             for y in years] for k in range(WEEKS)]


def _weekly(inflow: pd.Series, years: list[int]) -> np.ndarray:
    """(år, vecka): medelflöde i m³/s."""
    return np.array([[inflow[_week_start(y, k):][:7].mean() for k in range(WEEKS)] for y in years])


def compute_water_values(cascade: Cascade, until: pd.Timestamp, area: str = "SE3",
                         risk: tuple[float, float] = RISK_NEUTRAL) -> WaterValues:
    plants = cascade.plants
    upper = plants[0]
    volumes = np.arange(0, upper.v_max + GRID_STEP / 2, GRID_STEP)
    to_mm3 = HOURS_PER_WEEK * MM3_PER_M3S_HOUR

    # Scenarioår: hela år före until med både tillrinning och priser för varje vecka.
    prices = load_prices(HISTORY_START, until, area, freq="h")
    inflows = daily_inflows(cascade)
    years = [y for y in range(HISTORY_START.year + 1, until.year)
             if pd.Timestamp(year=y, month=12, day=31) < until and inflows.index[0].year <= y]
    scenarios = _price_scenarios(prices, years)
    years = [y for i, y in enumerate(years) if all(scenarios[k][i] for k in range(WEEKS))]
    scenarios = _price_scenarios(prices, years)

    # Alla år skalas med samma faktor till senaste årets prisnivå.
    level = float(prices[prices.index >= until - pd.Timedelta(days=LEVEL_DAYS)].mean())
    history_mean = float(np.mean([prices[str(y)].mean() for y in years]))
    scenarios = [[[cum * level / history_mean for cum in year] for year in week] for week in scenarios]

    flows = {p.name: _weekly(inflows[p.name], years) for p in plants}
    # Vattendomen: det övre magasinet måste släppa det som saknas för minimitappningen nedströms.
    min_release = np.zeros((len(years), WEEKS))
    local = np.zeros((len(years), WEEKS))
    for p in plants[1:]:
        local += flows[p.name]
        min_release = np.maximum(min_release, p.min_flow - local)

    bottleneck = min(p.q_max for p in plants)
    energy = np.array([p.energy_equivalent for p in plants])
    head = upper.head_factor((volumes[:, None] + volumes[None, :]) / 2)   # (startnivå, slutnivå)
    full_power = bottleneck * MM3_PER_M3S_HOUR * (energy[0] * head + energy[1:].sum())
    extra_power = (upper.q_max - bottleneck) * MM3_PER_M3S_HOUR * energy[0] * head
    # Veckor då kraftverken nedströms inte får korttidsreglera (veckans första dag avgör).
    steady = set().union(*(p.steady_months for p in plants[1:]))
    flat_weeks = np.array([_week_start(2001, k).month in steady for k in range(WEEKS)])
    flat_power = bottleneck * MM3_PER_M3S_HOUR * energy[1:].sum()

    values, guide = solve_sdp(volumes, flows[upper.name] * to_mm3, scenarios, bottleneck * MM3_PER_M3S_HOUR,
                              full_power, (upper.q_max - bottleneck) * MM3_PER_M3S_HOUR, extra_power,
                              min_release * to_mm3, risk, flat_power, flat_weeks)
    return WaterValues(volumes, values, guide, (years[0], years[-1]), level)


def cached_water_values(cascade: Cascade, until: pd.Timestamp, area: str = "SE3",
                        risk: tuple[float, float] = RISK_NEUTRAL) -> WaterValues:
    """Som compute_water_values, men sparad på disk så att varje månad bara räknas en gång
    (så länge koden och kaskaden är oförändrade)."""
    code = b"".join((Path(__file__).parent / f).read_bytes() for f in ("watervalues.py", "cascade.py", "data.py"))
    key = hashlib.sha1(code + repr(cascade).encode() + repr(risk).encode()).hexdigest()[:12]
    path = CACHE / "vattenvarden" / f"{area}_{until:%Y%m%d}_{key}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    wv = compute_water_values(cascade, until, area, risk)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(wv))
    return wv


def monthly_water_values(cascade: Cascade, start: pd.Timestamp, end: pd.Timestamp, area: str = "SE3",
                         risk: tuple[float, float] = RISK_NEUTRAL) -> MonthlyWaterValues:
    """Vattenvärden för varje månad i perioden, beräknade parallellt."""
    months = list(pd.date_range(start.normalize().replace(day=1), end, freq="MS"))
    with ProcessPoolExecutor(max_workers=min(len(months), os.cpu_count() or 1)) as pool:
        results = pool.map(cached_water_values, [cascade] * len(months), months, [area] * len(months),
                           [risk] * len(months))
        return MonthlyWaterValues(dict(zip(months, results)))
