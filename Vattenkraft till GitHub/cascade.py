"""Kaskaden: kraftverk, aggregat och magasin längs en reglerad älv.

Modellen bygger på Fortums kraftverk Trängslet, Åsen och Spjutmo i Österdalälven (SE3).
Källor:
- Fortum: effekt, fallhöjd och normal årsproduktion.
- Länsstyrelsen Dalarna, Dalälvens vattenkraftssystem (2017): Trängslets magasin (880 Mm³,
  regleringsamplitud 35 m) och tappning (0–250 m³/s), vattenhushållningsbestämmelserna för
  Åsen (minimitappning 21 m³/s, ändringstakt, inget effektkörande 1 juni–31 augusti), att
  Åsens magasin räcker till minimitappningen i fem dygn och att kraftverken nedströms Åsen
  saknar egen reglering och följer Åsen med 4–5 timmars fördröjning.
- ENTSO-E: Trängslet har tre aggregat (G1–G3). Spjutmo har ett Kaplanaggregat (Litostroj).
- SMHI: avrinningsområdena är kalibrerade så att tillrinningen stämmer med det uppmätta flödet
  vid Spjutmo kraftverk 1996–2025.
Antaganden: verkningsgradskurvor, antal aggregat i Åsen, gångtiden Trängslet–Åsen och
startkostnader. Väsa och Blyberg mellan Åsen och Spjutmo ägs gemensamt i separata bolag och
ingår inte.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

RHO_G = 9.81e-3                 # MW per (m³/s · m fallhöjd) vid verkningsgrad 1
MM3_PER_M3S_HOUR = 3600 / 1e6   # 1 m³/s under en timme = 0.0036 Mm³


@dataclass(frozen=True)
class Unit:
    """Ett aggregat (turbin + generator). Produktionskurvan har tre brytpunkter:
    minsta last, bästa verkningsgrad och maxflöde. Under minsta last står aggregatet."""
    q_max: float                    # m³/s
    head: float                     # m, fallhöjd vid fullt magasin
    start_cost: float               # € per start (slitage, underhåll)
    min_load: float = 0.4           # minsta flöde, andel av q_max
    best_load: float = 0.8          # flöde vid bästa verkningsgrad, andel av q_max
    efficiency: tuple[float, float, float] = (0.86, 0.93, 0.90)  # vid min, bästa, max

    @property
    def flows(self) -> np.ndarray:
        return np.array([self.min_load, self.best_load, 1.0]) * self.q_max

    @property
    def powers(self) -> np.ndarray:
        return RHO_G * self.head * np.array(self.efficiency) * self.flows

    @property
    def q_min(self) -> float:
        return self.flows[0]

    @property
    def p_min(self) -> float:
        return self.powers[0]

    @property
    def p_max(self) -> float:
        return self.powers[-1]

    def segments(self) -> list[tuple[float, float]]:
        """(bredd i m³/s, marginell effekt i MW per m³/s) för kurvan ovanför minsta last.
        Marginaleffekten måste vara avtagande, annars fyller lösaren segmenten i fel ordning."""
        q, p = self.flows, self.powers
        segs = [(q[i + 1] - q[i], (p[i + 1] - p[i]) / (q[i + 1] - q[i])) for i in range(len(q) - 1)]
        if any(b[1] > a[1] for a, b in zip(segs, segs[1:])):
            raise ValueError("Produktionskurvan ovanför minsta last måste vara konkav")
        return segs

    def power(self, q: np.ndarray) -> np.ndarray:
        return np.interp(q, self.flows, self.powers)


# Kaplanturbiner har flackare verkningsgradskurva och lägre minsta last än Francisturbiner.
def kaplan(q_max: float, head: float, start_cost: float) -> Unit:
    return Unit(q_max, head, start_cost, min_load=0.3, best_load=0.75, efficiency=(0.88, 0.93, 0.90))


@dataclass(frozen=True)
class Plant:
    """Kraftverk med identiska aggregat och ett eget magasin (eller damm)."""
    name: str
    unit: Unit
    n_units: int
    v_max: float                    # Mm³, regleringsvolym mellan sänknings- och dämningsgräns
    catchment_km2: float            # lokalt avrinningsområde (mellan detta och kraftverket ovanför)
    response_days: float            # hur trögt det lokala området svarar (sjöar och myrar dämpar)
    min_flow: float = 0.0           # m³/s, minimitappning enligt vattendom
    delay_to_next: int = 0          # h, gångtid till nästa kraftverk nedströms
    head_drop: float = 0.0          # m, så mycket lägre fallhöjden är vid tomt magasin än vid fullt
    ramp: float = 0.0               # m³/s per timme som tappningen högst får ändras (0 = fritt)
    steady_months: tuple[int, ...] = ()   # månader utan korttidsreglering: samma tappning hela dygnet
    steady_change: float = 0.0      # m³/s som tappningen högst får ändras mellan dygn under de månaderna

    @property
    def q_max(self) -> float:
        return self.n_units * self.unit.q_max

    @property
    def p_max(self) -> float:
        return self.n_units * self.unit.p_max

    @property
    def energy_equivalent(self) -> float:
        """MWh per Mm³ vid bästa verkningsgrad och full fallhöjd."""
        return RHO_G * self.unit.head * self.unit.efficiency[1] / MM3_PER_M3S_HOUR

    def head_factor(self, volume):
        """Fallhöjden relativt fullt magasin. I en dalgång växer volymen ungefär med
        kvadraten på djupet, så nivån sjunker som 1 - sqrt(V/Vmax) när magasinet töms."""
        if not self.head_drop:
            return np.ones_like(volume, dtype=float) if np.ndim(volume) else 1.0
        fill = np.clip(np.asarray(volume, dtype=float) / self.v_max, 0, 1)
        return 1 - self.head_drop / self.unit.head * (1 - np.sqrt(fill))

    def dispatch(self, flow: float, prefer_units: int | None = None) -> tuple[int, float, float]:
        """Fördelar ett önskat flöde på aggregaten. Returnerar (antal i drift, turbinflöde, förbitappning).
        Används planens antal aggregat om det går, annars det antal som ger mest effekt.
        Flöde under minsta last eller över total kapacitet går förbi turbinerna."""
        u = self.unit
        turbine = min(flow, self.q_max)
        feasible = [n for n in range(1, self.n_units + 1) if n * u.q_min <= turbine + 1e-9]
        if not feasible:
            return 0, 0.0, flow
        candidates = [n for n in feasible if turbine <= n * u.q_max + 1e-9]
        if prefer_units in candidates:
            n = prefer_units
        else:
            n = max(candidates, key=lambda k: k * u.power(turbine / k))
        return n, turbine, flow - turbine

    def power(self, turbine_flow: float, units: int, volume: float | None = None) -> float:
        if not units:
            return 0.0
        factor = 1.0 if volume is None else float(self.head_factor(volume))
        return factor * units * float(self.unit.power(turbine_flow / units))


@dataclass(frozen=True)
class Cascade:
    """Kraftverken i ordning uppströms -> nedströms."""
    name: str
    plants: tuple[Plant, ...]

    @property
    def max_delay(self) -> int:
        return max(p.delay_to_next for p in self.plants)

    def downstream_energy(self) -> np.ndarray:
        """MWh per Mm³ för vatten i varje magasin: vattnet passerar alla kraftverk nedströms."""
        e = np.array([p.energy_equivalent for p in self.plants])
        return np.cumsum(e[::-1])[::-1]

    def steady_days(self, start: pd.Timestamp, steps: int, step_h: float) -> np.ndarray:
        """(perioder, kraftverk): dygnets nummer räknat från start för perioder där kraftverket
        inte får korttidsreglera, annars -1."""
        times = pd.date_range(start, periods=steps, freq=pd.Timedelta(hours=step_h))
        day = ((times.normalize() - start.normalize()) // pd.Timedelta(days=1)).to_numpy()
        return np.column_stack([np.where(times.month.isin(p.steady_months), day, -1) for p in self.plants])


# Startkostnad ~7 €/MW per start (antagande).
OSTERDALALVEN = Cascade(
    name="Österdalälven: Trängslet, Åsen, Spjutmo",
    plants=(
        # 300 MW installerat, 142 m, 651 GWh/år, magasin 880 Mm³ och regleringsamplitud 35 m. Tre
        # aggregat (ENTSO-E) som tillsammans tar 250 m³/s. Verkningsgraden är kalibrerad mot
        # ENTSO-E: högst 290 MW uppmätt (2018–2026) ger 0,83 vid full last, och Fortums verkliga
        # tappning vintern 2025/26 gav 6,5 % mindre el än med 0,93 vid bästa last, alltså 0,87.
        Plant("Trängslet", Unit(q_max=250 / 3, head=142, start_cost=700,
                                efficiency=(0.80, 0.87, 290 / (RHO_G * 142 * 250))),
              n_units=3, v_max=880, catchment_km2=4230, response_days=6, delay_to_next=2, head_drop=35),
        # 24 MW, 24 m, 117 GWh/år. Två Kaplanaggregat (antagande). Magasinet räcker till
        # minimitappningen 21 m³/s i fem dygn, alltså cirka 9 Mm³. Tappningen får ändras högst
        # 30 m³/s per timme, och 1 juni–31 augusti inte korttidsregleras alls: då ändras den högst
        # 40 m³/s per dygn. Väsa, Blyberg och Spjutmo följer Åsen med 4–5 timmars fördröjning.
        Plant("Åsen", kaplan(q_max=56, head=24, start_cost=100), n_units=2, v_max=9,
              catchment_km2=280, response_days=2, delay_to_next=5, min_flow=21, ramp=30,
              steady_months=(6, 7, 8), steady_change=40),
        # 35 MW, 21,4 m, 138 GWh/år, ett Kaplanaggregat. Inget eget regleringsmagasin: dammen
        # hålls på jämn nivå (0,5 Mm³ räcker för att jämna ut inom någon timme). Rotälven
        # (knappt 900 km²) mynnar uppströms.
        Plant("Spjutmo", kaplan(q_max=185, head=21.4, start_cost=250), n_units=1, v_max=0.5,
              catchment_km2=1410, response_days=3),
    ),
)
