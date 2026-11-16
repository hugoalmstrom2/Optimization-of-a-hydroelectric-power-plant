"""Genomför en plan mot den faktiska tillrinningen, period för period och kraftverk för kraftverk.

Planen är gjord på prognoser, så verkligheten avviker. Driften reagerar som en
operatör: blir magasinet fullt ökas tappningen (turbin först, sedan spill), och blir
det tomt minskas den (spill först, sedan turbin). Flöden under minsta last kan inte köras
genom turbinen och går förbi den, men har planen dragits ned under minsta last för att
magasinet tog slut står turbinen och vattnet sparas. Effekten räknas med fallhöjden vid
aktuell magasinsnivå. Alla strategier genomförs med samma regler.

Vattenhushållningsbestämmelserna gäller också: planerad tappning från ett kraftverk med
begränsad ändringstakt jämnas ut innan den genomförs (samma tappning hela dygnet när
korttidsreglering är förbjuden). Ändringar som beror på att magasinet blir fullt eller tomt
får göras, som i verkligheten.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from cascade import MM3_PER_M3S_HOUR, Cascade
from model import FCR_D_SHARE, FCR_N_SHARE, State


@dataclass
class Outcome:
    """Utfall per period och kraftverk."""
    flow: np.ndarray        # turbinflöde m³/s
    spill: np.ndarray       # förbitappning m³/s
    units: np.ndarray       # aggregat i drift
    starts: np.ndarray      # aggregatstarter
    power: np.ndarray       # MW
    volume: np.ndarray      # Mm³ vid periodens slut
    shortfall: np.ndarray   # m³/s under minimitappningen (ska vara noll)


def regulate(cascade: Cascade, turbine_plan: np.ndarray, spill_plan: np.ndarray, state: State,
             step_h: float = 0.25, steady: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Planen justerad efter bestämmelserna om ändringstakt: dygnsmedel när korttidsreglering är
    förbjuden (högst steady_change från föregående dygn), annars högst ramp per timme."""
    turbine, spill = turbine_plan.astype(float).copy(), spill_plan.astype(float).copy()
    for p, plant in enumerate(cascade.plants):
        if not plant.ramp and not plant.steady_months:
            continue
        planned = turbine[:, p] + spill[:, p]
        release = np.empty_like(planned)
        label = np.full(len(planned), -1) if steady is None else np.asarray(steady)[:len(planned), p]
        previous = float(state.release_history[-1, p])
        t = 0
        while t < len(planned):
            if label[t] >= 0:
                end = t + int(np.argmax(label[t:] != label[t])) if np.any(label[t:] != label[t]) else len(planned)
                level = planned[t:end].mean()
                if plant.steady_change:
                    level = np.clip(level, previous - plant.steady_change, previous + plant.steady_change)
                release[t:end] = level
                t = end
            else:
                step = plant.ramp * step_h if plant.ramp else np.inf
                release[t] = np.clip(planned[t], previous - step, previous + step)
                t += 1
            previous = release[t - 1]
        changed = np.abs(release - planned) > 1e-6
        turbine[changed, p] = np.minimum(release[changed], plant.q_max)
        spill[changed, p] = release[changed] - turbine[changed, p]
    return turbine, spill


def execute(cascade: Cascade, turbine_plan: np.ndarray, spill_plan: np.ndarray,
            units_plan: np.ndarray | None, inflow: np.ndarray, state: State,
            step_h: float = 0.25, steady: np.ndarray | None = None) -> tuple[Outcome, State]:
    turbine_plan, spill_plan = regulate(cascade, turbine_plan, spill_plan, state, step_h, steady)
    H, n_plants = turbine_plan.shape
    c = MM3_PER_M3S_HOUR * step_h
    out = Outcome(*(np.zeros((H, n_plants)) for _ in Outcome.__dataclass_fields__))
    release = np.vstack([state.release_history, np.zeros((H, n_plants))])
    offset = len(state.release_history)
    volume = state.volume.astype(float).copy()
    units_prev = state.units_on.copy()

    for t in range(H):
        for p, plant in enumerate(cascade.plants):
            arriving = inflow[t, p]
            if p > 0:
                arriving += release[offset + t - round(cascade.plants[p - 1].delay_to_next / step_h), p - 1]
            turbine, spill = turbine_plan[t, p], spill_plan[t, p]

            v_next = volume[p] + c * (arriving - turbine - spill)
            if v_next > plant.v_max:
                excess = (v_next - plant.v_max) / c
                more = min(excess, max(plant.q_max - turbine, 0))
                turbine, spill = turbine + more, spill + excess - more
            elif v_next < 0:
                deficit = -v_next / c
                less = min(deficit, spill)
                spill -= less
                turbine = max(turbine - (deficit - less), 0)
            if turbine + spill < plant.min_flow:
                available = volume[p] / c + arriving  # mer än så kan inte släppas den här perioden
                spill = max(min(plant.min_flow, available) - turbine, spill)
                out.shortfall[t, p] = max(plant.min_flow - turbine - spill, 0)

            n, turbine, bypass = plant.dispatch(turbine, None if units_plan is None else int(units_plan[t, p]))
            if n == 0 and bypass > 0 and turbine_plan[t, p] >= plant.unit.q_min:
                # Planen ville köra, men magasinet räckte bara till ett flöde under minsta last. Då står
                # turbinen och vattnet sparas, utom det som minimitappningen kräver eller magasinet inte rymmer.
                # (Planerade små flöden, t.ex. för minimitappningen nedströms, släpps som vanligt.)
                overflow = (volume[p] + c * (arriving - spill) - plant.v_max) / c
                bypass = min(bypass, max(plant.min_flow - spill, overflow, 0))
            spill += bypass
            out.power[t, p] = plant.power(turbine, n, volume[p])
            volume[p] += c * (arriving - turbine - spill)
            release[offset + t, p] = turbine + spill

            out.flow[t, p], out.spill[t, p], out.units[t, p] = turbine, spill, n
            out.starts[t, p] = max(n - units_prev[p], 0)
            out.volume[t, p] = volume[p]
            units_prev[p] = n

    new_state = State(volume=volume, release_history=release[-offset:], units_on=units_prev.astype(int))
    return out, new_state


def reserve_room(cascade: Cascade, outcome: Outcome) -> np.ndarray:
    """Kaskadens marginaler för stödtjänster per period givet körningen (MW): uppåt för aggregat i
    drift och för alla aggregat, nedåt till minsta last och till noll, samt taken för FCR-N och FCR-D."""
    room = np.zeros((len(outcome.power), 6))
    for p, plant in enumerate(cascade.plants):
        head = plant.head_factor(outcome.volume[:, p])
        power = outcome.power[:, p]
        cap_on = head * plant.unit.p_max * outcome.units[:, p]
        room += np.column_stack([
            np.clip(cap_on - power, 0, None),
            np.clip(head * plant.unit.p_max * plant.n_units - power, 0, None),
            np.clip(power - head * plant.unit.p_min * outcome.units[:, p], 0, None),
            power,
            FCR_N_SHARE * cap_on,
            FCR_D_SHARE * cap_on,
        ])
    return room


def deliverable_reserves(sold: np.ndarray, room: np.ndarray) -> np.ndarray:
    """Hur mycket av sålda FCR-N, FCR-D upp/ned och mFRR upp/ned (MW) som ryms i marginalerna.
    FCR-N tas först, sedan FCR-D och sist mFRR av det som blir kvar."""
    up_on, up_all, down_min, down_all, n_cap, d_cap = room.T
    fcr_n = np.minimum.reduce([sold[:, 0], up_on, down_min, n_cap])
    fcr_up = np.minimum.reduce([sold[:, 1], up_on - fcr_n, d_cap])
    fcr_down = np.minimum.reduce([sold[:, 2], down_min - fcr_n, d_cap])
    mfrr_up = np.clip(np.minimum(sold[:, 3], up_all - fcr_n - fcr_up), 0, None)
    mfrr_down = np.clip(np.minimum(sold[:, 4], down_all - fcr_n - fcr_down), 0, None)
    return np.column_stack([fcr_n, fcr_up, fcr_down, mfrr_up, mfrr_down])
