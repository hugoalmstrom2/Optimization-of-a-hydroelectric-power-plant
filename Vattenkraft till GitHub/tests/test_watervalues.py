import numpy as np
import pandas as pd

import pytest

from watervalues import HOURS_PER_WEEK, RISK_AVERSE, WEEKS, WaterValues, risk_adjusted, solve_sdp


def cumulative(prices: np.ndarray) -> np.ndarray:
    return np.concatenate([[0], np.cumsum(np.sort(prices)[::-1])])


def synthetic_sdp(price_level: float = 50.0):
    """Litet magasin, vårflod och tre år där pris och tillrinning hör ihop."""
    volumes = np.arange(0, 101, 10.0)
    week = np.arange(WEEKS)
    flood = 40 * np.exp(-((week - 20) / 3) ** 2)
    inflow = np.array([5 + flood * f for f in (0.6, 1.0, 1.4)])
    rng = np.random.default_rng(1)
    cum = [[[cumulative(price_level * rng.uniform(0.5, 1.5, HOURS_PER_WEEK)) for _ in range(2)] for _ in range(3)]
           for _ in range(WEEKS)]
    values, guide = solve_sdp(volumes, inflow, cum, hourly_release=0.2, full_power=50.0)
    return WaterValues(volumes, values, guide, (2000, 2002), price_level)


def test_water_values_are_concave_and_non_negative():
    wv = synthetic_sdp()
    for k in range(WEEKS):
        slopes = np.diff(wv.values[k]) / np.diff(wv.volumes)
        assert np.all(slopes >= -1e-6)                  # mer vatten är aldrig värt mindre
        assert np.all(np.diff(slopes) <= 1e-6 * np.abs(slopes).max())  # avtagande marginalvärde


def test_water_value_is_low_before_the_spring_flood():
    wv = synthetic_sdp()
    winter, before_flood = pd.Timestamp("2026-01-10"), pd.Timestamp("2026-05-10")
    assert wv.marginal(before_flood)[5] < wv.marginal(winter)[5]


def test_guide_fills_during_the_flood():
    wv = synthetic_sdp()
    assert wv.guide[24, 1] > wv.guide[16, 1]


def test_zero_prices_give_zero_water_values():
    wv = synthetic_sdp(price_level=0.0)
    assert np.allclose(wv.values, 0)


def test_price_and_inflow_of_the_same_year_are_paired():
    """Samma priser kopplade till andra år ger andra vattenvärden: parningen spelar roll."""
    volumes = np.arange(0, 101, 10.0)
    inflow = np.array([np.full(WEEKS, 2.0), np.full(WEEKS, 30.0)])          # torrt och vått år
    low, high = (cumulative(np.full(HOURS_PER_WEEK, p)) for p in (20.0, 80.0))
    dry_expensive = [[[high], [low]] for _ in range(WEEKS)]
    dry_cheap = [[[low], [high]] for _ in range(WEEKS)]
    a, _ = solve_sdp(volumes, inflow, dry_expensive, 0.2, 50.0)
    b, _ = solve_sdp(volumes, inflow, dry_cheap, 0.2, 50.0)
    assert not np.allclose(a, b)


def test_cvar_is_the_mean_of_the_worst_outcomes():
    samples = np.array([[30.0], [0.0], [20.0], [10.0]])
    weights = np.full(4, 0.25)
    assert risk_adjusted(samples, weights, 0.0, 0.2)[0] == pytest.approx(15.0)
    assert risk_adjusted(samples, weights, 1.0, 0.25)[0] == pytest.approx(0.0)
    assert risk_adjusted(samples, weights, 1.0, 0.375)[0] == pytest.approx(10 / 3)   # 0 och en halv tia
    assert risk_adjusted(samples, weights, 0.5, 0.25)[0] == pytest.approx(7.5)


def test_risk_aversion_lowers_the_value_and_sells_water_earlier():
    """Framtida intäkter är osäkra, så en riskavert planering säljer hellre nu."""
    volumes = np.arange(0, 101, 10.0)
    week = np.arange(WEEKS)
    season = 1 + 0.5 * np.cos(2 * np.pi * week / WEEKS)                       # dyrt på vintern
    inflow = np.array([2 + 40 * np.exp(-((week - 20) / 3) ** 2) * f for f in (0.3, 1.0, 1.7)])
    rng = np.random.default_rng(2)
    cum = [[[cumulative(50 * season[k] * level * rng.uniform(0.7, 1.3, HOURS_PER_WEEK))]
            for level in (1.5, 1.0, 0.7)] for k in range(WEEKS)]
    min_release = np.full_like(inflow, 3.0)
    neutral, neutral_guide = solve_sdp(volumes, inflow, cum, 0.2, 50.0, min_release=min_release)
    averse, averse_guide = solve_sdp(volumes, inflow, cum, 0.2, 50.0, min_release=min_release, risk=RISK_AVERSE)
    assert np.all(averse <= neutral + 1e-6)
    assert averse_guide[:, 1].mean() < neutral_guide[:, 1].mean()


def test_min_release_makes_the_last_water_more_valuable():
    """Med en vattendom som kräver tappning blir de sista Mm³ i magasinet mer värda."""
    volumes = np.arange(0, 101, 10.0)
    inflow = np.tile(5 + 40 * np.exp(-((np.arange(WEEKS) - 20) / 3) ** 2), (3, 1))
    cum = [[[cumulative(np.full(HOURS_PER_WEEK, 50.0))] for _ in range(3)] for _ in range(WEEKS)]
    free, _ = solve_sdp(volumes, inflow, cum, 0.2, 50.0)
    bound, _ = solve_sdp(volumes, inflow, cum, 0.2, 50.0, min_release=np.full_like(inflow, 8.0))
    assert np.diff(bound[10])[0] > np.diff(free[10])[0]
