import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# pandas 3 gör copy-on-write till standard: data som tas ut ur en Series eller DataFrame är
# skrivskyddad. Testerna körs med samma beteende även med pandas 2.
if pd.__version__.startswith("2."):
    pd.set_option("mode.copy_on_write", True)

from cascade import Cascade, Plant, Unit  # noqa: E402
from model import State  # noqa: E402


@pytest.fixture
def small_cascade() -> Cascade:
    """Två kraftverk: ett magasin och en damm med minimitappning, gångtid 2 h."""
    return Cascade("test", (
        Plant("Övre", Unit(q_max=50, head=60, start_cost=100), n_units=2, v_max=20,
              catchment_km2=1000, response_days=3, delay_to_next=2),
        Plant("Nedre", Unit(q_max=60, head=20, start_cost=50), n_units=2, v_max=1.0,
              catchment_km2=300, response_days=2, min_flow=20),
    ))


@pytest.fixture
def start_state(small_cascade) -> State:
    return State(volume=np.array([10.0, 0.5]), release_history=np.full((2, 2), 30.0),
                 units_on=np.array([0, 1]))


@pytest.fixture
def price_48h() -> np.ndarray:
    hour = np.arange(48) % 24
    return 40 + 30 * np.exp(-((hour - 18) / 3) ** 2) + 15 * np.exp(-((hour - 8) / 2) ** 2)
