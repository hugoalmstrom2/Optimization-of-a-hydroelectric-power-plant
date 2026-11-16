from types import SimpleNamespace

import numpy as np
import pulp
import pytest

from backtest import clear_bids
from cascade import MM3_PER_M3S_HOUR
from model import RETRY_SEEDS, Terminal, _overlaps, _solve, optimize, time_blocks
from simulation import execute


def flat_terminal(cascade, value_per_mm3=5000.0) -> Terminal:
    edges = [np.array([0.0, p.v_max / 2, p.v_max]) for p in cascade.plants]
    slopes = [np.array([1.2, 0.8]) * value_per_mm3 for _ in cascade.plants]
    return Terminal(edges, slopes)


def test_time_blocks_are_quarters_then_hours_then_four_hours():
    blocks = time_blocks(7 * 96, step_h=0.25)
    assert sum(blocks) == 7 * 96
    assert blocks[:96] == [1] * 96 and blocks[96:120] == [4] * 24 and set(blocks[120:]) == {16}
    assert time_blocks(48, step_h=1.0) == [1] * 48


def test_arrival_weights_shift_by_delay_and_sum_to_one():
    edges = np.concatenate([[0], np.cumsum(time_blocks(3 * 96))])
    weights = _overlaps(edges, delay=24, n_history=24)          # 6 h i kvartar
    assert weights[0] == ({}, {-24: 1.0})
    assert weights[30] == ({6: 1.0}, {})
    for from_blocks, from_history in weights:
        assert np.isclose(sum(from_blocks.values()) + sum(from_history.values()), 1.0)


def test_terminal_rejects_non_concave_value_function():
    with pytest.raises(ValueError):
        Terminal([np.array([0.0, 1.0, 2.0])], [np.array([1.0, 2.0])])


def test_terminal_value_is_piecewise_linear():
    t = Terminal([np.array([0.0, 10.0, 20.0])], [np.array([3.0, 1.0])])
    assert t.value(np.array([15.0])) == pytest.approx(10 * 3 + 5 * 1)
    assert t.scaled(2).value(np.array([15.0])) == pytest.approx(70)


def test_plan_respects_bounds_and_water_balance(small_cascade, start_state, price_48h):
    inflow = np.tile([40.0, 8.0], (48, 1))
    plan = optimize(small_cascade, price_48h, inflow, start_state, flat_terminal(small_cascade), step_h=1.0)
    for p, plant in enumerate(small_cascade.plants):
        assert np.all(plan.volume[:, p] >= -1e-6) and np.all(plan.volume[:, p] <= plant.v_max + 1e-6)
    released = plan.flow[:, 0] + plan.spill[:, 0]
    expected = start_state.volume[0] + MM3_PER_M3S_HOUR * (inflow[:, 0] - released).sum()
    assert plan.volume[-1, 0] == pytest.approx(expected, abs=1e-6)
    assert np.all(plan.flow[:, 1] + plan.spill[:, 1] >= small_cascade.plants[1].min_flow - 1e-6)


def test_simulation_reproduces_plan_when_inflow_matches_forecast(small_cascade, start_state, price_48h):
    """Med perfekt prognos ska genomförandet ge samma flöden, volymer och (minst) samma effekt."""
    inflow = np.tile([40.0, 8.0], (48, 1))
    plan = optimize(small_cascade, price_48h, inflow, start_state, flat_terminal(small_cascade), step_h=1.0)
    outcome, _ = execute(small_cascade, plan.flow[:24], plan.spill[:24], plan.units[:24], inflow[:24],
                         start_state, step_h=1.0)
    assert np.allclose(outcome.flow, plan.flow[:24], atol=1e-4)
    assert np.allclose(outcome.volume, plan.volume[:24], atol=1e-4)
    assert np.all(outcome.power >= plan.power[:24] - 1e-4)
    assert outcome.shortfall.max() == 0


def test_commitment_is_followed_when_feasible(small_cascade, start_state, price_48h):
    inflow = np.tile([40.0, 8.0], (48, 1))
    free = optimize(small_cascade, price_48h, inflow, start_state, flat_terminal(small_cascade), step_h=1.0)
    commitment = np.full(24, free.power[:24].sum(axis=1).mean())       # jämn leverans hela dygnet
    plan = optimize(small_cascade, price_48h, inflow, start_state, flat_terminal(small_cascade), step_h=1.0,
                    commitment=commitment)
    deviation = np.abs(plan.power[:24].sum(axis=1) - commitment)
    assert deviation.sum() < 0.01 * commitment.sum()                  # startläget kan kosta någon timme
    assert np.allclose(deviation[2:], 0, atol=0.5)


def test_bid_curve_never_offers_less_at_a_higher_price():
    prices = np.array([[20.0, 50.0], [40.0, 60.0], [60.0, 70.0]])     # tre scenarier, två perioder
    power = np.array([[10.0, 30.0], [5.0, 20.0], [40.0, 50.0]])        # mellersta scenariot bryter ordningen
    commitment = clear_bids(prices, power, np.array([40.0, 65.0]), water_value=10.0)
    assert commitment[0] == pytest.approx(10.0)                        # 5 höjs till 10 (monoton kurva)
    assert commitment[1] == pytest.approx(40.0)                        # mittemellan 30 och 50


def test_bid_falls_to_zero_at_the_water_value_below_all_scenarios():
    prices = np.array([[20.0, 50.0], [40.0, 60.0], [60.0, 70.0]])
    power = np.array([[10.0, 30.0], [20.0, 40.0], [40.0, 50.0]])
    below = clear_bids(prices, power, np.array([15.0, 5.0]), water_value=10.0)
    assert below[0] == pytest.approx(5.0)                              # halvvägs mellan vattenvärdet och 20
    assert below[1] == 0.0
    expensive_water = clear_bids(prices, power, np.array([19.5, 45.0]), water_value=80.0)
    assert expensive_water[0] == pytest.approx(5.0)                    # under lägsta scenariot: snabbt ned
    assert expensive_water[1] == 0.0


class ScriptedProblem:
    """Ett problem där lösaren svarar med de givna statusarna i tur och ordning."""

    def __init__(self, *statuses):
        self.statuses, self.attempts = list(statuses), []

    def solve(self, options):
        self.attempts.append(options)
        status = self.statuses.pop(0)
        return SimpleNamespace(status=status, has_solution=status == pulp.LpSolveStatus.Optimal)


def test_numerical_solver_error_is_retried_with_other_seeds(monkeypatch):
    monkeypatch.setattr(pulp, "HiGHS", lambda **options: options)
    error, optimal = pulp.LpSolveStatus.Undefined, pulp.LpSolveStatus.Optimal
    prob = ScriptedProblem(error, error, optimal)
    assert _solve(prob, 0.005, 300).has_solution
    assert [a.get("random_seed") for a in prob.attempts] == [None, *RETRY_SEEDS[:2]]
    assert all(a["timeLimit"] == 300 for a in prob.attempts)            # felet beror inte på tiden

    hopeless = ScriptedProblem(*[error] * (1 + len(RETRY_SEEDS)))
    with pytest.raises(RuntimeError):
        _solve(hopeless, 0.005, 300)


def test_time_limit_without_solution_gives_one_longer_attempt(monkeypatch):
    monkeypatch.setattr(pulp, "HiGHS", lambda **options: options)
    prob = ScriptedProblem(pulp.LpSolveStatus.TimeLimit, pulp.LpSolveStatus.TimeLimit)
    with pytest.raises(RuntimeError):
        _solve(prob, 0.005, 300)
    assert [a["timeLimit"] for a in prob.attempts] == [300, 3000]
    assert all("random_seed" not in a for a in prob.attempts)
