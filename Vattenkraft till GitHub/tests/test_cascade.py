import numpy as np
import pytest

from cascade import OSTERDALALVEN, Unit


def test_production_curve_is_concave_above_min_load():
    for plant in OSTERDALALVEN.plants:
        slopes = [s for _, s in plant.unit.segments()]
        assert all(a >= b for a, b in zip(slopes, slopes[1:]))


def test_capacity_matches_fortums_figures():
    """Åsen och Spjutmo enligt Fortum; Trängslet enligt högsta uppmätta effekt (ENTSO-E), 300 MW installerat."""
    assert [round(p.p_max) for p in OSTERDALALVEN.plants] == [290, 24, 35]


def test_cascade_follows_the_water_court_rules():
    """Länsstyrelsen Dalarna (2017): minimitappning 21 m³/s från Åsen, magasin för fem dygns
    minimitappning, inget effektkörande nedströms Åsen 1 juni–31 augusti."""
    trangslet, asen, spjutmo = OSTERDALALVEN.plants
    assert trangslet.q_max == pytest.approx(250) and trangslet.head_drop == 35
    assert asen.min_flow == 21 and asen.v_max == pytest.approx(5 * 86400 * 21 / 1e6, rel=0.05)
    assert asen.steady_months == (6, 7, 8) and asen.ramp == 30 and asen.steady_change == 40
    assert spjutmo.min_flow == 0 and spjutmo.v_max < 1


def test_power_matches_breakpoints():
    unit = Unit(q_max=100, head=30, start_cost=0)
    assert np.allclose(unit.power(unit.flows), unit.powers)
    assert unit.p_max == unit.powers[-1]


def test_head_factor_falls_as_the_reservoir_empties():
    upper = OSTERDALALVEN.plants[0]
    assert upper.head_factor(upper.v_max) == pytest.approx(1.0)
    assert upper.head_factor(0.0) == pytest.approx(1 - upper.head_drop / upper.unit.head)
    assert upper.power(200, 3, volume=upper.v_max / 2) < upper.power(200, 3, volume=upper.v_max)
    assert OSTERDALALVEN.plants[1].head_factor(0.0) == 1.0     # dammarna: konstant fallhöjd


def test_dispatch_below_min_load_bypasses_turbines():
    plant = OSTERDALALVEN.plants[0]
    n, turbine, bypass = plant.dispatch(plant.unit.q_min / 2)
    assert (n, turbine) == (0, 0.0) and bypass == plant.unit.q_min / 2


def test_dispatch_above_capacity_spills_the_rest():
    plant = OSTERDALALVEN.plants[0]
    n, turbine, bypass = plant.dispatch(plant.q_max + 15)
    assert n == plant.n_units and turbine == plant.q_max and np.isclose(bypass, 15)


def test_dispatch_keeps_planned_units_when_feasible():
    plant = OSTERDALALVEN.plants[1]
    flow = 1.5 * plant.unit.q_min       # bara ett aggregat går: två skulle kräva 2·q_min
    assert plant.dispatch(flow, prefer_units=2)[0] == 1
    flow = 2.2 * plant.unit.q_min       # går med både ett och två aggregat
    assert plant.dispatch(flow, prefer_units=2)[0] == 2
    assert plant.dispatch(flow, prefer_units=1)[0] == 1


def test_downstream_energy_accumulates():
    e = OSTERDALALVEN.downstream_energy()
    assert np.isclose(e[-1], OSTERDALALVEN.plants[-1].energy_equivalent)
    assert np.all(np.diff(e) < 0)
