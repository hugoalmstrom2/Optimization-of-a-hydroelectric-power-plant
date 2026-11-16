"""Linjärprogrammeringsmodell (PuLP) för veckoplanering av ett vattenkraftverk.

Formulering (timupplösning, t = 1..T, k = segment på produktionskurvan):

    max   sum_t  pris_t * P_t
    där   P_t = sum_k e_k * q_tk                      (konkav produktionskurva)
          V_t = V_{t-1} + c * (I_t - Q_t - S_t)       (vattenbalans, Q_t = sum_k q_tk)
          Q_t + S_t >= minimitappning
          V_min <= V_t <= V_max,  0 <= q_tk <= bredd_k,  S_t >= 0
          V_T >= V_slut

Eftersom marginaleffekten e_k avtar med flödet fyller lösaren alltid det mest
effektiva segmentet först, så inga heltalsvariabler behövs.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pulp

# 1 m³/s under en timme = 3600 m³ = 0.0036 Mm³
MM3_PER_M3S_HOUR = 3600 / 1e6
# Liten straffkostnad på spill så att lösaren inte spiller vatten när det är
# likgiltigt (t.ex. vid exakt nollpris). € per (m³/s·h).
SPILL_PENALTY = 1e-3


@dataclass(frozen=True)
class Plant:
    """Kraftverk och magasin. Standardvärdena motsvarar ett mellanstort älvkraftverk.

    Produktionskurvan anges som brytpunkter (flöde, effekt). Med fallhöjd 40 m är
    P = 9.81 * 40 * η * Q / 1000 MW. Bästa verkningsgrad (92 %) nås vid 160 m³/s,
    vid fullt flöde 200 m³/s har den sjunkit till 89,5 %.
    """
    flow_points: tuple[float, ...] = (0.0, 160.0, 200.0)  # m³/s
    power_points: tuple[float, ...] = (0.0, 57.8, 70.2)   # MW
    v_min: float = 2.0      # Mm³, sänkningsgräns
    v_max: float = 22.0     # Mm³, dämningsgräns
    v_start: float = 12.0   # Mm³
    v_end: float = 12.0     # Mm³, mål vid veckans slut (t.ex. från säsongsplaneringen)
    min_flow: float = 15.0  # m³/s, minimitappning enligt vattendom

    @property
    def q_max(self) -> float:
        return self.flow_points[-1]

    @property
    def p_max(self) -> float:
        return self.power_points[-1]

    @property
    def energy_equivalent(self) -> float:
        """MWh per Mm³ vid bästa verkningsgrad."""
        return self.segments()[0][1] / MM3_PER_M3S_HOUR

    def segments(self) -> list[tuple[float, float]]:
        """(bredd i m³/s, marginell effekt i MW per m³/s) för varje segment av kurvan."""
        q, p = self.flow_points, self.power_points
        segs = [(q[i + 1] - q[i], (p[i + 1] - p[i]) / (q[i + 1] - q[i])) for i in range(len(q) - 1)]
        slopes = [slope for _, slope in segs]
        if any(b > a for a, b in zip(slopes, slopes[1:])):
            raise ValueError("Produktionskurvan måste vara konkav (avtagande marginell effekt), "
                             "annars fyller LP-lösaren segmenten i fel ordning.")
        return segs

    def power(self, flow) -> np.ndarray:
        """Effekt i MW för givet turbinflöde, interpolerat längs produktionskurvan."""
        return np.interp(flow, self.flow_points, self.power_points)


def optimize(price: np.ndarray, inflow: np.ndarray, plant: Plant) -> pd.DataFrame:
    """Maximerar periodens intäkt givet timpriser (€/MWh) och tillrinning (m³/s).

    Returnerar en tabell per timme med turbinflöde, spill, magasinsvolym, effekt
    och vattenvärde (€/MWh) ur dualvärdet på vattenbalansen.
    """
    T = len(price)
    hours = range(T)
    segs = plant.segments()
    c = MM3_PER_M3S_HOUR

    prob = pulp.LpProblem("vattenkraft_vecka", pulp.LpMaximize)
    q = {(t, k): prob.add_variable(f"q_{t}_{k}", lowBound=0, upBound=width)
         for t in hours for k, (width, _) in enumerate(segs)}
    s = {t: prob.add_variable(f"s_{t}", lowBound=0) for t in hours}
    v = {t: prob.add_variable(f"v_{t}", lowBound=plant.v_min, upBound=plant.v_max) for t in hours}

    flow = {t: pulp.lpSum(q[t, k] for k in range(len(segs))) for t in hours}
    power = {t: pulp.lpSum(slope * q[t, k] for k, (_, slope) in enumerate(segs)) for t in hours}

    # Intäkt från spotförsäljning; timupplösning så MW * 1 h = MWh.
    prob += (pulp.lpSum(price[t] * power[t] for t in hours)
             - SPILL_PENALTY * pulp.lpSum(s.values()))

    for t in hours:
        v_prev = v[t - 1] if t > 0 else plant.v_start
        prob += v[t] - v_prev + c * (flow[t] + s[t]) == c * inflow[t], f"vattenbalans_{t}"
        prob += flow[t] + s[t] >= plant.min_flow, f"minimitappning_{t}"
    prob += v[T - 1] >= plant.v_end, "slutmagasin"

    stats = prob.solve(pulp.HiGHS(msg=False))
    if stats.status != pulp.LpSolveStatus.Optimal:
        raise RuntimeError(f"Optimeringen hittade ingen optimal lösning (status: {stats.status.name})")

    turbine = np.clip([flow[t].value() for t in hours], 0, None)
    # Dualvärdet på vattenbalansen är värdet av en extra Mm³ i magasinet den timmen.
    # Omräknat till €/MWh vid bästa verkningsgrad blir det kraftverkets vattenvärde:
    # lönar det sig inte att sälja till timpriset sparas vattnet.
    dual = np.array([prob.get_constraint_by_name(f"vattenbalans_{t}").pi for t in hours])
    return pd.DataFrame({
        "flow": turbine,
        "spill": np.clip([s[t].value() for t in hours], 0, None),
        "volume": [v[t].value() for t in hours],
        "power": plant.power(turbine),
        "water_value": dual / plant.energy_equivalent,
    })
