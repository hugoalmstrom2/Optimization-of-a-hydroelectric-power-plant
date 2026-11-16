import numpy as np
import pytest

from backtest import hourly_offer
from model import FCR_D_SHARE, Terminal, optimize, optimize_scenarios
from simulation import Outcome, deliverable_reserves, reserve_room


def flat_terminal(cascade, value_per_mm3=5000.0) -> Terminal:
    edges = [np.array([0.0, p.v_max / 2, p.v_max]) for p in cascade.plants]
    slopes = [np.array([1.2, 0.8]) * value_per_mm3 for _ in cascade.plants]
    return Terminal(edges, slopes)


def plan_with_reserves(small_cascade, start_state, price_48h, reserve_price, **options):
    inflow = np.tile([40.0, 8.0], (48, 1))
    return optimize(small_cascade, price_48h, inflow, start_state, flat_terminal(small_cascade), step_h=1.0,
                    reserve_price=np.tile(reserve_price, (48, 1)), **options)


def test_reserves_fit_within_head_room_and_foot_room(small_cascade, start_state, price_48h):
    plan = plan_with_reserves(small_cascade, start_state, price_48h, [20.0, 5.0, 5.0, 10.0, 2.0])
    p_max = sum(p.p_max for p in small_cascade.plants)
    fcr_n, fcr_up, fcr_down, mfrr_up, mfrr_down = plan.reserves.T
    produced = plan.power.sum(axis=1)
    assert np.all(produced + fcr_n + fcr_up + mfrr_up <= p_max + 1e-6)
    assert np.all(produced - fcr_n - fcr_down - mfrr_down >= -1e-6)
    assert mfrr_up.max() > 0                            # ledig kapacitet säljs som mFRR upp


def test_reserve_cap_and_fixed_volumes_are_respected(small_cascade, start_state, price_48h):
    cap = np.array([3.0, 5.0, 5.0, 20.0, 5.0])
    capped = plan_with_reserves(small_cascade, start_state, price_48h, [20.0, 5.0, 5.0, 10.0, 2.0], reserve_cap=cap)
    assert np.all(capped.reserves <= cap + 1e-6)
    fixed = np.tile([0.0, 0.0, 0.0, 10.0, 0.0], (24, 1))
    plan = plan_with_reserves(small_cascade, start_state, price_48h, [20.0, 5.0, 5.0, 10.0, 2.0], reserve_fixed=fixed)
    assert np.allclose(plan.reserves[:24], fixed, atol=1e-6)
    fixed[:, 0] = np.nan                                                # FCR-N inte såld än: fri att välja
    plan = plan_with_reserves(small_cascade, start_state, price_48h, [20.0, 5.0, 5.0, 10.0, 2.0], reserve_fixed=fixed)
    assert np.allclose(plan.reserves[:24, 1:], fixed[:, 1:], atol=1e-6) and plan.reserves[:24, 0].max() > 0


def test_two_stage_model_shares_the_reserves_sold_before_the_price_is_known(small_cascade, start_state, price_48h):
    prices = np.vstack([0.5 * price_48h, price_48h, 1.5 * price_48h])
    plans = optimize_scenarios(small_cascade, prices, np.array([0.25, 0.5, 0.25]), np.tile([40.0, 8.0], (48, 1)),
                               start_state, flat_terminal(small_cascade), step_h=1.0,
                               reserve_price=np.tile([20.0, 5.0, 5.0, 10.0, 2.0], (48, 1)), shared=(3, 4),
                               shared_steps=24)
    for plan in plans[1:]:
        assert np.allclose(plan.reserves[:24, 3:], plans[0].reserves[:24, 3:], atol=1e-6)
    assert not np.allclose(plans[0].power, plans[2].power)          # körplanerna får skilja sig åt


def test_two_stage_production_forms_a_bid_curve(small_cascade, start_state, price_48h):
    rng = np.random.default_rng(5)
    prices = np.vstack([price_48h + rng.normal(0, 15, 48) for _ in range(4)])
    plans = optimize_scenarios(small_cascade, prices, np.full(4, 0.25), np.tile([40.0, 8.0], (48, 1)), start_state,
                               flat_terminal(small_cascade), step_h=1.0, bid_steps=24)
    power = np.array([p.power[:24].sum(axis=1) for p in plans])
    for t in range(24):
        order = np.argsort(prices[:, t])
        assert np.all(np.diff(power[order, t]) >= -1e-6)            # aldrig mindre vid högre pris


def test_two_stage_model_with_one_scenario_is_the_ordinary_model(small_cascade, start_state, price_48h):
    reserve_price = [20.0, 5.0, 5.0, 10.0, 2.0]
    single = plan_with_reserves(small_cascade, start_state, price_48h, reserve_price)
    joint = optimize_scenarios(small_cascade, price_48h[None, :], np.array([1.0]), np.tile([40.0, 8.0], (48, 1)),
                               start_state, flat_terminal(small_cascade), step_h=1.0,
                               reserve_price=np.tile(reserve_price, (48, 1)), shared=(3, 4), shared_steps=24)
    assert joint[0].objective == pytest.approx(single.objective, rel=0.01)


def test_reserves_are_constant_within_each_hour(small_cascade, start_state):
    state = start_state
    state.release_history = np.full((8, 2), 30.0)      # 2 h gångtid i kvartar
    price = np.tile(40 + 30 * np.sin(np.arange(96) / 96 * 2 * np.pi), 2)
    plan = optimize(small_cascade, price, np.tile([40.0, 8.0], (192, 1)), state, flat_terminal(small_cascade),
                    reserve_price=np.tile([20.0, 5.0, 5.0, 10.0, 2.0], (192, 1)))
    hourly = plan.reserves[:96].reshape(24, 4, 5)
    assert np.allclose(hourly, hourly[:, :1, :])


def test_deliverable_reserves_follow_the_dispatch(small_cascade):
    upper = small_cascade.plants[0]
    power = np.array([[0.0, 0.0], [upper.unit.p_max, 0.0]])          # stilla resp. ett aggregat fullt
    outcome = Outcome(flow=np.zeros((2, 2)), spill=np.zeros((2, 2)), units=np.array([[0, 0], [1, 0]]),
                      starts=np.zeros((2, 2)), power=power, volume=np.array([[10.0, 0.5], [10.0, 0.5]]),
                      shortfall=np.zeros((2, 2)))
    available = deliverable_reserves(np.full((2, 5), 1e3), reserve_room(small_cascade, outcome))
    total_p_max = sum(p.p_max for p in small_cascade.plants)
    assert available[0, :3].sum() == 0 and available[0, 3] == pytest.approx(total_p_max)   # bara mFRR upp
    assert available[1, 0] == 0                      # fullt: ingen marginal uppåt för FCR-N
    assert available[1, 2] == pytest.approx(min(FCR_D_SHARE, 1 - upper.unit.min_load) * upper.unit.p_max)


def test_fcr_goes_first_but_unsold_fcr_does_not_crowd_out_mfrr():
    room = np.array([[10.0, 10.0, 50.0, 50.0, 10.0, 10.0]])
    only_mfrr = deliverable_reserves(np.array([[0.0, 0.0, 0.0, 10.0, 0.0]]), room)
    both = deliverable_reserves(np.array([[0.0, 10.0, 0.0, 10.0, 0.0]]), room)
    assert only_mfrr[0, 3] == 10.0
    assert both[0, 1] == 10.0 and both[0, 3] == 0.0


def test_the_planned_reserves_are_deliverable_at_the_planned_dispatch(small_cascade, start_state, price_48h):
    plan = plan_with_reserves(small_cascade, start_state, price_48h, [20.0, 5.0, 5.0, 10.0, 2.0])
    outcome = Outcome(flow=plan.flow, spill=plan.spill, units=plan.units, starts=np.zeros_like(plan.units),
                      power=plan.power, volume=np.tile(start_state.volume, (len(plan.power), 1)),
                      shortfall=np.zeros_like(plan.flow))
    delivered = deliverable_reserves(plan.reserves, reserve_room(small_cascade, outcome))
    assert plan.reserves.sum() > 0
    assert np.allclose(delivered, plan.reserves, atol=1e-4)


def test_hourly_offer_uses_the_lowest_quarter():
    room = np.tile([20.0, 60.0, 20.0, 60.0, 5.0, 10.0], (8, 1))
    room[2, 1] = 30.0
    offer = hourly_offer(room, cap=np.array([10.0, 10.0, 10.0, 40.0, 10.0]))
    assert np.all(offer[:, :3] == [5.0, 10.0, 10.0])
    assert np.all(offer[:4, 3] == 15.0) and np.all(offer[4:, 3] == 40.0)
