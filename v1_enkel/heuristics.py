"""Tumregler att jämföra optimeringen mot, och simulering av dem genom magasinet.

Båda tumreglerna tappar lika mycket vatten som optimeringen under veckan (de
landar på samma slutmagasin), så skillnaden i intäkt beror enbart på när och
hur vattnet används.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from model import MM3_PER_M3S_HOUR, Plant


def water_to_release(inflow: np.ndarray, plant: Plant) -> float:
    """Vatten som ska tappas under perioden för att nå slutmålet, i (m³/s)·h."""
    return (plant.v_start - plant.v_end) / MM3_PER_M3S_HOUR + inflow.sum()


def even_release(price: np.ndarray, inflow: np.ndarray, plant: Plant) -> np.ndarray:
    """Jämn tappning: samma flöde varje timme, ingen hänsyn till priset."""
    q = water_to_release(inflow, plant) / len(inflow)
    return np.full(len(inflow), np.clip(q, plant.min_flow, plant.q_max))


def daily_peak(price: np.ndarray, inflow: np.ndarray, plant: Plant) -> np.ndarray:
    """Dygnstopp: dela veckans vatten lika per dygn och kör fullt under dygnets
    dyraste timmar. Övriga timmar släpps bara minimitappningen."""
    days = len(price) // 24
    per_day = water_to_release(inflow, plant) / days
    plan = np.full(len(price), plant.min_flow)
    for d in range(days):
        hours = np.arange(24 * d, 24 * (d + 1))
        left = per_day - 24 * plant.min_flow
        for t in hours[np.argsort(-price[hours], kind="stable")]:
            if left <= 0:
                break
            extra = min(plant.q_max - plant.min_flow, left)
            plan[t] += extra
            left -= extra
    return plan


def simulate(planned_flow: np.ndarray, inflow: np.ndarray, plant: Plant) -> pd.DataFrame:
    """Kör en planerad tappning genom magasinet som en driftoperatör skulle göra:
    slår magasinet i dämningsgränsen ökas tappningen och det som inte ryms i
    turbinen spills; slår det i sänkningsgränsen minskas tappningen."""
    c = MM3_PER_M3S_HOUR
    flow = np.asarray(planned_flow, dtype=float).copy()
    spill = np.zeros_like(flow)
    volume = np.empty_like(flow)
    v = plant.v_start
    for t in range(len(flow)):
        v_next = v + c * (inflow[t] - flow[t])
        if v_next > plant.v_max:
            excess = (v_next - plant.v_max) / c
            more = min(excess, plant.q_max - flow[t])
            flow[t] += more
            spill[t] = excess - more
        elif v_next < plant.v_min:
            flow[t] -= (plant.v_min - v_next) / c
        v += c * (inflow[t] - flow[t] - spill[t])
        volume[t] = v
    return pd.DataFrame({"flow": flow, "spill": spill, "volume": volume, "power": plant.power(flow)})
