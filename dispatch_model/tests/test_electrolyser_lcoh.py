"""scripts/electrolyser_lcoh.py: the optimum is the brute-force optimum, and the 8000 h case is the k cheapest hours."""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import electrolyser_lcoh as el  # noqa: E402

E = 55.0                      # kWh per kg
CHARGE = 8760 * 50.0          # EUR per MW-year: 50 EUR for each priced hour, 400 EUR over an 8-hour "year"


def brute_force(prices: np.ndarray, charge: float, e: float) -> float:
    c = charge * len(prices) / 8760.0
    return min((c + sum(s)) * e / (1000.0 * len(s))
               for r in range(1, len(prices) + 1) for s in itertools.combinations(prices, r))


def test_crf_at_8_percent_over_20_years_and_at_zero_rate():
    assert el.crf(0.08, 20) == pytest.approx(0.101852, abs=1e-6)
    assert el.crf(0.0, 20) == pytest.approx(0.05)


@pytest.mark.parametrize("seed", range(6))
def test_optimum_equals_the_best_subset_of_hours(seed):
    p = np.random.default_rng(seed).uniform(-30.0, 120.0, 8)
    r = el.optimum(p, CHARGE, E)
    assert r["lcoh"] == pytest.approx(brute_force(p, CHARGE, E), rel=1e-12)
    assert r["capex_part"] + r["power_part"] == pytest.approx(r["lcoh"], rel=1e-12)
    assert r["threshold"] == pytest.approx(np.sort(p)[r["hours"] - 1])
    assert r["load_factor"] == pytest.approx(r["hours"] / len(p))


def test_fixed_hours_run_the_k_cheapest_prorated_to_the_priced_hours():
    p = np.array([90.0, -10.0, 40.0, 5.0, 70.0, 0.0, 20.0, 110.0])
    r = el.optimum(p, CHARGE, E, fixed_hours=5 * 8760 / 8)    # 5 of the 8 priced hours
    cheapest5 = np.sort(p)[:5].sum()
    c = CHARGE * 8 / 8760
    assert r["fixed_lcoh"] == pytest.approx((c + cheapest5) * E / (1000.0 * 5))
    assert r["fixed_avg_price_paid"] == pytest.approx(cheapest5 / 5)
    assert r["fixed_capex_part"] + r["fixed_power_part"] == pytest.approx(r["fixed_lcoh"])
    assert r["fixed_lcoh"] >= r["lcoh"]


def test_all_negative_prices_run_every_hour():
    p = -np.arange(1.0, 9.0)
    r = el.optimum(p, CHARGE, E)
    assert r["hours"] == len(p)
    assert r["lcoh"] == pytest.approx(r["baseload_lcoh"])
