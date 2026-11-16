"""Vattenhushållningsbestämmelser (ändringstakt och förbud mot korttidsreglering), den nya
tillrinningsprognosen och deterministisk budgivning."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from backtest import clear_bids
from cascade import OSTERDALALVEN, Cascade
from forecast import INFLOW_PERSISTENCE, inflow_normal, inflow_path
from model import Terminal, optimize
from simulation import regulate
from watervalues import HOURS_PER_WEEK, WEEKS, solve_sdp


def flat_terminal(cascade):
    edges = [np.array([0.0, p.v_max / 2, p.v_max]) for p in cascade.plants]
    return Terminal(edges, [np.array([4000.0, 2000.0]), np.array([1500.0, 1000.0])])


def regulated(cascade: Cascade, **rules) -> Cascade:
    """Samma kaskad, men det nedre kraftverket får bestämmelser om ändringstakt."""
    return replace(cascade, plants=(cascade.plants[0], replace(cascade.plants[1], **rules)))


def test_steady_days_mark_summer_days_for_asen_only():
    labels = OSTERDALALVEN.steady_days(pd.Timestamp("2026-05-31"), 3 * 96, 0.25)
    assert np.all(labels[:, 0] == -1) and np.all(labels[:, 2] == -1)       # Trängslet och Spjutmo
    assert np.all(labels[:96, 1] == -1)                                   # 31 maj: fritt
    assert np.all(labels[96:192, 1] == 1) and np.all(labels[192:, 1] == 2)  # 1–2 juni: dygn 1 och 2


def test_milp_keeps_the_release_steady_within_each_day(small_cascade, start_state, price_48h):
    cascade = regulated(small_cascade, steady_months=(7,), steady_change=10.0)
    steady = np.column_stack([np.full(48, -1), np.repeat([0, 1], 24)])
    inflow = np.tile([40.0, 8.0], (48, 1))
    plan = optimize(cascade, price_48h, inflow, start_state, flat_terminal(cascade), step_h=1.0, steady=steady)
    release = plan.flow[:, 1] + plan.spill[:, 1]
    assert np.ptp(release[:24]) < 1e-4 and np.ptp(release[24:]) < 1e-4
    assert abs(release[0] - 30.0) <= 10.0 + 1e-4                     # högst 10 m³/s från dygnet före
    assert abs(release[24] - release[23]) <= 10.0 + 1e-4


def test_milp_cannot_meet_the_day_change_limit_first_and_drop_later(small_cascade, start_state, price_48h):
    """Dygnet före tappades 30 m³/s och ändringen får vara högst 5, men vattnet uppströms är så
    dyrt att planen helst vill ned till minimitappningen 20. Att klara gränsen första timmen och
    sänka sedan ska kosta lika mycket som att bryta mot den hela dygnet: genomförandet tappar ändå
    dygnets medelnivå, klippt till 25."""
    cascade = regulated(small_cascade, steady_months=(7,), steady_change=5.0)
    steady = np.column_stack([np.full(48, -1), np.repeat([0, 1], 24)])
    inflow = np.tile([40.0, 8.0], (48, 1))
    expensive_upstream = Terminal([np.array([0.0, 10.0, 20.0]), np.array([0.0, 0.5, 1.0])],
                                  [np.array([3e5, 3e5]), np.array([1500.0, 1000.0])])
    plan = optimize(cascade, price_48h, inflow, start_state, expensive_upstream, step_h=1.0, steady=steady)
    release = plan.flow[:24, 1] + plan.spill[:24, 1]
    assert np.ptp(release) < 1e-3
    assert release.min() >= 25.0 - 1e-3


def test_milp_respects_the_ramp_limit(small_cascade, start_state, price_48h):
    cascade = regulated(small_cascade, ramp=4.0)
    inflow = np.tile([40.0, 8.0], (48, 1))
    free = optimize(small_cascade, price_48h, inflow, start_state, flat_terminal(small_cascade), step_h=1.0)
    plan = optimize(cascade, price_48h, inflow, start_state, flat_terminal(cascade), step_h=1.0)
    release = np.r_[30.0, plan.flow[:, 1] + plan.spill[:, 1]]
    assert np.abs(np.diff(release)).max() <= 4.0 + 1e-4
    assert np.abs(np.diff(np.r_[30.0, free.flow[:, 1] + free.spill[:, 1]])).max() > 4.0   # villkoret binder


def test_regulate_limits_the_ramp_and_averages_steady_days(small_cascade, start_state):
    cascade = regulated(small_cascade, ramp=4.0)
    plan = np.column_stack([np.zeros(4), [30.0, 50.0, 50.0, 10.0]])
    turbine, spill = regulate(cascade, plan, np.zeros_like(plan), start_state, step_h=1.0)
    assert np.allclose(turbine[:, 1] + spill[:, 1], [30, 34, 38, 34])     # högst 4 m³/s per timme från 30
    steady_cascade = regulated(small_cascade, steady_months=(7,), steady_change=10.0)
    steady = np.column_stack([np.full(4, -1), np.zeros(4, int)])
    turbine, spill = regulate(steady_cascade, plan, np.zeros_like(plan), start_state, 1.0, steady)
    assert np.allclose(turbine[:, 1] + spill[:, 1], 35.0)          # dygnsmedlet 35 ligger inom 30 ± 10
    unchanged, _ = regulate(small_cascade, plan, np.zeros_like(plan), start_state, step_h=1.0)
    assert np.allclose(unchanged, plan)                          # inga bestämmelser: planen som den är


def test_inflow_forecast_starts_at_the_observation_and_returns_to_the_normal():
    days = pd.date_range("2000-01-01", "2009-12-31")
    season = 50 + 40 * np.sin(2 * np.pi * (days.dayofyear - 100) / 365)
    daily = pd.DataFrame({"A": season}, index=days)
    normal = inflow_normal(daily, pd.Timestamp("2010-01-01"))
    assert normal.shape == (365, 1)
    last = pd.Timestamp("2010-03-01")
    noon = last + pd.Timedelta(hours=12)
    observed = pd.DataFrame({"A": [normal.loc[60, "A"] + 10.0]}, index=[noon])
    path = inflow_path(observed.reindex(pd.date_range(noon, periods=2, freq="15min")).ffill(), last,
                       pd.Timestamp("2010-03-02"), 96 * 30, normal)[:, 0]
    assert path[0] - normal.loc[61, "A"] == pytest.approx(10.0 * INFLOW_PERSISTENCE ** 0.5, abs=0.5)
    assert abs(path[-1] - normal.loc[90, "A"]) < 10.0 * INFLOW_PERSISTENCE ** 25   # avvikelsen klingar av
    persistence = inflow_path(observed.reindex(pd.date_range(noon, periods=2, freq="15min")).ffill(), last,
                              pd.Timestamp("2010-03-02"), 96, None)
    assert np.allclose(persistence, observed.iloc[0, 0])


def test_single_scenario_bids_the_planned_volume_down_to_the_water_value():
    bid = clear_bids(np.array([[60.0, 60.0, 60.0]]), np.array([[100.0, 100.0, 100.0]]),
                     np.array([20.0, 45.0, 90.0]), water_value=40.0)
    assert list(bid) == [0.0, 100.0, 100.0]


def test_flat_summer_weeks_lower_the_water_value():
    """Får kraftverken nedströms bara medelpriset på sommaren är vattnet mindre värt."""
    volumes = np.arange(0, 101, 10.0)
    inflow = np.tile(5 + 40 * np.exp(-((np.arange(WEEKS) - 20) / 3) ** 2), (2, 1))
    hours = np.arange(HOURS_PER_WEEK)
    peaky = 50 + 30 * np.sin(2 * np.pi * hours / 24)
    cum = [[[np.concatenate([[0], np.cumsum(np.sort(peaky)[::-1])])] for _ in range(2)] for _ in range(WEEKS)]
    summer = np.zeros(WEEKS, dtype=bool)
    summer[22:35] = True
    free, _ = solve_sdp(volumes, inflow, cum, 0.2, 50.0)
    flat, _ = solve_sdp(volumes, inflow, cum, 0.2, 50.0, flat_power=20.0, flat_weeks=summer)
    assert np.all(flat <= free + 1e-6) and flat.sum() < free.sum()
