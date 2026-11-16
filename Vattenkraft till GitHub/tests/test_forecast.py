import numpy as np
import pandas as pd
import pytest

from forecast import LEVEL_DECAY, price_path, price_scenarios, scenario_weights


def test_known_prices_are_kept_and_deviation_decays_toward_profile():
    index = pd.date_range("2026-01-05", periods=7 * 24, freq="h")
    known = pd.Series(np.tile(np.arange(24, dtype=float), 7), index=index)
    known.iloc[-1] += 70                                 # sista kända timmen ligger högt över profilen
    path, n_known = price_path(known, index[-24], 48)

    assert n_known == 24 and np.allclose(path[:24], known.iloc[-24:])
    profile_23 = 23 + 70 / 7                             # snittet för timme 23 inkluderar avvikelsen
    deviation = known.iloc[-1] - profile_23
    assert path[24] == pytest.approx(0 + deviation * LEVEL_DECAY)
    assert path[47] == pytest.approx(profile_23 + deviation * LEVEL_DECAY ** 24)


def test_quarter_hour_profile_has_96_slots():
    index = pd.date_range("2026-01-05", periods=7 * 96, freq="15min")
    known = pd.Series(np.tile(np.arange(96, dtype=float), 7), index=index)
    path, n_known = price_path(known, index[-1] + pd.Timedelta(minutes=15), 96)
    assert n_known == 0 and np.allclose(path, np.arange(96), atol=1e-9)


def test_scenarios_spread_the_first_day_and_keep_the_rest():
    rng = np.random.default_rng(3)
    index = pd.date_range("2026-01-01", periods=60 * 24, freq="h")
    known = pd.Series(50 + 10 * np.sin(np.arange(len(index)) / 24 * 2 * np.pi) + rng.normal(0, 8, len(index)),
                      index=index)
    day = index[-1] + pd.Timedelta(hours=1)
    scenarios = price_scenarios(known, day, 72, n=5)
    base, _ = price_path(known, day, 72)
    assert scenarios.shape == (5, 72)
    assert np.all(np.diff(scenarios[:, :24].mean(axis=1)) >= 0)     # ordnade från lågt till högt
    assert np.allclose(scenarios[:, 24:], base[24:])
    errors = []
    for k in range(1, 29):
        past = day - pd.Timedelta(days=k)
        forecast, _ = price_path(known[known.index < past], past, 24)
        errors.append((known[past:past + pd.Timedelta(hours=23)].to_numpy() - forecast).mean())
    spread = scenarios[:, :24].mean(axis=1) - base[:24].mean()
    assert spread[0] == pytest.approx(min(errors)) and spread[-1] == pytest.approx(max(errors))


def test_level_model_knows_that_weekends_are_cheaper():
    index = pd.date_range("2026-01-05", periods=120 * 24, freq="h")            # börjar en måndag
    weekend = np.asarray(index.weekday >= 5)
    known = pd.Series(50 + 10 * np.sin(np.arange(len(index)) / 24 * 2 * np.pi) + np.where(weekend, -20, 20),
                      index=index)
    saturday = pd.Timestamp("2026-05-02")
    path, _ = price_path(known[known.index < saturday], saturday, 24)
    assert saturday.weekday() == 5
    assert path.mean() == pytest.approx(30, abs=3)              # profilen över sju dygn ensam ger cirka 59


def test_end_scenarios_weigh_half():
    assert np.allclose(scenario_weights(5), [1 / 8, 1 / 4, 1 / 4, 1 / 4, 1 / 8])
