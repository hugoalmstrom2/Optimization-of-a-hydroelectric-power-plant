from dataclasses import replace
from types import SimpleNamespace

import numpy as np

from cascade import MM3_PER_M3S_HOUR
from heuristics import (INFLOW_MARGIN, _min_load_block, _required_release, buffer_holds, daily_peak, water_value_bid,
                        weekly_peak)
from simulation import execute


def run(cascade, state, plan, inflow):
    return execute(cascade, plan, np.zeros_like(plan), None, inflow, state, step_h=1.0)


def test_execution_conserves_water(small_cascade, start_state):
    rng = np.random.default_rng(0)
    plan = rng.uniform(0, 100, size=(24, 2))
    inflow = rng.uniform(5, 60, size=(24, 2))
    outcome, state = run(small_cascade, start_state, plan, inflow)
    released = outcome.flow + outcome.spill
    upstream_arrivals = np.concatenate([start_state.release_history[:, 0], released[:, 0]])[:24]
    arrivals = np.column_stack([np.zeros(24), upstream_arrivals]) + inflow
    expected = start_state.volume + MM3_PER_M3S_HOUR * (arrivals - released).sum(axis=0)
    assert np.allclose(state.volume, expected)
    for p, plant in enumerate(small_cascade.plants):
        assert np.all(outcome.volume[:, p] >= -1e-9) and np.all(outcome.volume[:, p] <= plant.v_max + 1e-9)


def test_quarter_steps_conserve_water_too(small_cascade, start_state):
    state = start_state
    state.release_history = np.full((8, 2), 30.0)                    # 2 h gångtid i kvartar
    plan = np.full((96, 2), 35.0)
    inflow = np.tile([40.0, 25.0], (96, 1))
    outcome, new = execute(small_cascade, plan, np.zeros_like(plan), None, inflow, state, step_h=0.25)
    assert np.isclose(new.volume[0], state.volume[0] + 0.25 * MM3_PER_M3S_HOUR * (40 - 35) * 96)


def test_planned_small_flow_is_released_but_a_cut_run_is_stored(small_cascade, start_state):
    inflow = np.tile([5.0, 25.0], (24, 1))
    small, _ = run(small_cascade, start_state, np.tile([10.0, 25.0], (24, 1)), inflow)
    assert np.allclose(small.spill[:, 0], 10)          # planerat flöde under minsta last släpps förbi
    start_state.volume[0] = 0.01                        # nästan tomt: planen på 100 m³/s kan inte köras
    cut, _ = run(small_cascade, start_state, np.tile([100.0, 25.0], (24, 1)), inflow)
    assert cut.spill[:, 0].sum() == 0                   # inget förbi turbinen: vattnet sparas ...
    q_min = small_cascade.plants[0].unit.q_min
    assert np.all((cut.flow[:, 0] == 0) | (cut.flow[:, 0] >= q_min - 1e-9))   # ... tills det räcker till minsta last
    assert cut.flow[:, 0].max() >= q_min


def test_full_reservoir_spills_instead_of_overflowing(small_cascade, start_state):
    start_state.volume[0] = small_cascade.plants[0].v_max
    outcome, state = run(small_cascade, start_state, np.zeros((24, 2)), np.tile([500.0, 0.0], (24, 1)))
    assert state.volume[0] <= small_cascade.plants[0].v_max + 1e-9
    assert outcome.spill[:, 0].sum() > 0


def test_min_flow_is_met_when_water_is_available(small_cascade, start_state):
    outcome, _ = run(small_cascade, start_state, np.zeros((24, 2)), np.tile([40.0, 25.0], (24, 1)))
    assert np.all(outcome.flow[:, 1] + outcome.spill[:, 1] >= small_cascade.plants[1].min_flow - 1e-9)
    assert outcome.shortfall.max() == 0


def test_min_flow_shortfall_is_recorded_when_the_pond_runs_dry(small_cascade, start_state):
    """Ingen tappning uppströms och för lite lokal tillrinning: dammen töms och underskottet redovisas."""
    outcome, state = run(small_cascade, start_state, np.zeros((24, 2)), np.tile([40.0, 10.0], (24, 1)))
    released = outcome.flow[:, 1] + outcome.spill[:, 1]
    assert np.allclose(released + outcome.shortfall[:, 1], small_cascade.plants[1].min_flow)
    assert outcome.shortfall[-1, 1] > 0 and state.volume[1] < 1e-9


def test_required_release_covers_downstream_min_flow(small_cascade):
    floor = _required_release(small_cascade, np.tile([40.0, 8.0], (24, 1)))
    # minimitappning nedströms minus lokal tillrinning, med marginal för att prognosen slår fel
    assert np.allclose(floor[:, 0], 20 - (1 - INFLOW_MARGIN) * 8)
    assert np.allclose(floor[:, 1], 20)


def test_rule_meets_min_flow_when_local_inflow_is_lower_than_forecast(small_cascade, start_state):
    """Med tom damm nedströms klaras minimitappningen bara av det som släpps uppströms (och då kan
    flödet inte köras i block). Utan marginal blev det för lite när den lokala tillrinningen blev
    lägre än prognosen."""
    forecast = np.tile([13.0, 8.0], (24, 1))           # lite vatten uppströms: planen följer golvet
    actual = forecast * [1.0, 1 - INFLOW_MARGIN / 2]
    start_state.volume[1] = 0.0
    start_state.release_history[:] = 20 - actual[0, 1]  # det som redan är på väg räcker precis
    plan = daily_peak(small_cascade, np.arange(24, dtype=float), start_state, forecast,
                      start_state.volume.copy(), step_h=1.0, min_load_blocks=False)
    outcome, _ = run(small_cascade, start_state, plan, actual)
    assert outcome.shortfall.max() == 0


def test_small_flows_run_as_min_load_blocks_instead_of_past_the_turbine(small_cascade, start_state):
    """Golvet för minimitappningen nedströms är under minsta last. I stället för att släppa det förbi
    turbinen körs ett aggregat på minsta last de dyraste timmarna, och dammen nedströms jämnar ut."""
    price, inflow = np.arange(24, dtype=float), np.tile([15.0, 8.0], (24, 1))
    upper = small_cascade.plants[0]
    plans = {blocks: daily_peak(small_cascade, price, start_state, inflow, start_state.volume.copy(), step_h=1.0,
                                min_load_blocks=blocks) for blocks in (False, True)}
    blocked = plans[True][:, 0]
    assert not np.any((blocked > 1e-9) & (blocked < upper.unit.q_min - 1e-9))
    assert blocked.sum() >= plans[False][:, 0].sum() - 1e-9                 # minst lika mycket vatten
    assert blocked[price < 7].max() == 0 and blocked[price >= 7].min() >= upper.unit.q_min - 1e-9
    outcomes = {blocks: run(small_cascade, start_state, plan, inflow)[0] for blocks, plan in plans.items()}
    assert outcomes[False].spill[:, 0].sum() > 0 and outcomes[True].spill[:, 0].sum() == 0
    assert outcomes[True].power[:, 0].sum() > outcomes[False].power[:, 0].sum()
    assert outcomes[True].shortfall.max() == 0 and buffer_holds(small_cascade, outcomes[True])


def test_min_load_block_always_runs_the_periods_that_reach_downstream_tomorrow():
    """Vatten från dygnets sista timmar når nästa kraftverk först nästa dygn. Ett glapp där kunde
    nästa dygn inte rätta, så de timmarna körs även när priset är lägst då."""
    out = _min_load_block(np.full(8, 10.0), 20.0, price=np.arange(8.0)[::-1], tail=2)
    assert np.array_equal(out, [20, 20, 0, 0, 0, 0, 20, 20])


def test_buffer_check_rejects_shortfall_and_a_nearly_empty_pond(small_cascade):
    ok = SimpleNamespace(shortfall=np.zeros((4, 2)), volume=np.full((4, 2), 0.5))
    assert buffer_holds(small_cascade, ok)
    short = SimpleNamespace(shortfall=np.array([[0, 0], [0, 0.1], [0, 0], [0, 0]]), volume=ok.volume)
    assert not buffer_holds(small_cascade, short)
    low = SimpleNamespace(shortfall=ok.shortfall, volume=np.array([[9, 0.5], [9, 0.01], [9, 0.5], [9, 0.5]]))
    assert not buffer_holds(small_cascade, low)


def test_daily_peak_runs_in_the_most_expensive_hours(small_cascade, start_state):
    price = np.arange(24, dtype=float)
    inflow = np.tile([60.0, 8.0], (24, 1))
    plan = daily_peak(small_cascade, price, start_state, inflow, start_state.volume.copy(), step_h=1.0)
    top = np.argsort(-price)[:3]
    assert np.all(plan[top, 0] == small_cascade.plants[0].q_max)
    assert np.isclose(plan[:, 0].sum(), inflow[:, 0].sum())  # på styrkurvan: släpper det som rinner till


def test_daily_peak_can_run_above_downstream_capacity(small_cascade, start_state):
    lower = small_cascade.plants[1]
    narrow = replace(small_cascade, plants=(small_cascade.plants[0], replace(lower, unit=replace(lower.unit, q_max=30))))
    price, inflow = np.arange(24, dtype=float), np.tile([60.0, 8.0], (24, 1))
    capped = daily_peak(narrow, price, start_state, inflow, start_state.volume.copy(), step_h=1.0)
    full = daily_peak(narrow, price, start_state, inflow, start_state.volume.copy(), step_h=1.0, cap_downstream=False)
    assert capped[:, 0].max() == narrow.plants[1].q_max      # 2 x 30 m³/s nedströms
    assert full[:, 0].max() == narrow.plants[0].q_max


def test_water_value_bid_has_two_steps(small_cascade, start_state):
    lower = small_cascade.plants[1]
    narrow = replace(small_cascade, plants=(small_cascade.plants[0], replace(lower, unit=replace(lower.unit, q_max=30))))
    energy = narrow.downstream_energy()
    water_value = 60.0 * energy[0]                     # 60 €/MWh när allt vatten går genom turbinerna
    second = water_value / (energy[0] - narrow.plants[1].energy_equivalent)
    price = np.arange(24, dtype=float) * 5 + 2.5       # inga priser exakt på trösklarna
    inflow = np.tile([40.0, 25.0], (24, 1))           # lokal tillrinning klarar minimitappningen nedströms
    plan = water_value_bid(narrow, price, start_state, inflow, start_state.volume.copy(), water_value, step_h=1.0)
    assert np.all(plan[price < 60, 0] == 0)
    assert np.all(plan[(price >= 60) & (price < second), 0] == narrow.plants[1].q_max)
    assert np.all(plan[price >= second, 0] == narrow.plants[0].q_max)


def test_weekly_peak_moves_water_to_the_expensive_day(small_cascade, start_state):
    price = np.concatenate([np.full(24, 100.0), np.full(6 * 24, 20.0)]) + np.tile(np.arange(24) * 0.1, 7)
    inflow = np.tile([30.0, 8.0], (7 * 24, 1))
    week = weekly_peak(small_cascade, price, start_state, inflow, start_state.volume.copy(), step_h=1.0)
    day = daily_peak(small_cascade, price[:24], start_state, inflow[:24], start_state.volume.copy(), step_h=1.0,
                     cap_downstream=False)
    assert week[:, 0].sum() > 3 * day[:, 0].sum()           # dygnet får en stor del av veckans vatten


def test_daily_peak_uses_min_load_when_water_only_covers_downstream_min_flow(small_cascade, start_state):
    """Räcker vattnet bara till minimitappningen nedströms körs ett aggregat på minsta last hela dygnet."""
    inflow = np.tile([20.0, 8.0], (24, 1))
    plan = daily_peak(small_cascade, np.arange(24, dtype=float), start_state, inflow,
                      start_state.volume.copy(), step_h=1.0)
    assert np.allclose(plan[:, 0], small_cascade.plants[0].unit.q_min)
