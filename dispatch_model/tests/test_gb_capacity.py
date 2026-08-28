"""`io.gb_capacity` — unfreezing Britain's fleet, and giving it the cables it actually has.

Two defects, both silent, both found by measurement rather than by reading the code.

FIRST: GB's installed capacity is a single snapshot. `entsoe_installed_capacity` holds exactly twelve rows
for GB, all stamped 2019-01-01, and they were served for every year 2019-2026 — so the 2025 backtest ran
Britain's 2019 fleet, with 9.23 GW of nuclear against a real 6.04 and 8.28 GW of coal against a real zero.
GB is the only zone affected; every ENTSO-E zone moves year to year. ENTSO-E stopped publishing British
capacity after Brexit and Elexon has no capacity register, so there is nothing to update from.

SECOND, and only visible once the first was fixed: GB is wired to Norway and Ireland, and neither is a
modelled zone. Removing the phantom plant exposed it — in all 37 VoLL hours of the 2025 backtest GB imports
5281 MW, identical to the megawatt, with every modelled border binding at 100 %, against a stack holding
8.7 GW spare. It runs out of CABLE, not plant.

The properties pinned here are the ones whose failure would be silent: a trajectory that stops moving, a
new-build step that lands in the wrong year, an import block that undercuts the domestic stack, and any of
it leaking into a zone that is not GB.
"""
from __future__ import annotations

import pytest

from dispatch_model.io import gb_capacity as gc


def test_the_fleet_actually_moves_between_years():
    """The whole defect was a snapshot that never changed. Nuclear must fall, then rise on new build."""
    assert gc.capacity_mw("nuclear", 2019) > gc.capacity_mw("nuclear", 2025) > gc.capacity_mw("nuclear", 2030)
    assert gc.capacity_mw("nuclear", 2037) > gc.capacity_mw("nuclear", 2030), \
        "Hinkley Point C and Sizewell C must show up as a RISE — a retirement-only curve is the old bug"


def test_the_2030_agr_cliff_is_present():
    """Hartlepool, Heysham 1, Heysham 2 and Torness all end in March 2030 — 3.2 GW leaving at once. A
    smooth glide would hide the single largest step in Britain's generation fleet this decade."""
    assert gc.capacity_mw("nuclear", 2029) - gc.capacity_mw("nuclear", 2030) > 2500.0


def test_coal_is_gone_by_2025_and_stays_gone():
    """Ratcliffe-on-Soar, the last coal plant in Great Britain, closed in September 2024."""
    assert gc.capacity_mw("coal", 2025) == pytest.approx(0.0, abs=1e-9)
    assert gc.capacity_mw("coal", 2040) == pytest.approx(0.0, abs=1e-9)
    assert gc.capacity_mw("coal", 2019) > 8000.0


def test_the_external_import_block_follows_commissioning_dates():
    """North Sea Link energised in October 2021 and Greenlink in 2025. Handing the 2019 backtest 2.9 GW of
    cable that did not exist would manufacture adequacy in exactly the years used to calibrate."""
    assert gc.ext_import_mw(2019) == pytest.approx(1000.0)
    assert gc.ext_import_mw(2021) == pytest.approx(2400.0)      # + North Sea Link
    assert gc.ext_import_mw(2025) == pytest.approx(2900.0)      # + Greenlink
    assert gc.ext_import_mw(2019) < gc.ext_import_mw(2025)


def test_the_import_block_is_dearer_than_britain_s_own_worst_gas():
    """It represents the LAST cables to fire. Priced below the domestic stack it would import Nordic hydro
    into GB's ordinary merit order and make a zone already 36 % below the market cheaper still; the point
    is to remove VoLL hours, not to remove price."""
    from dispatch_model.stacks.costs import EFF_RANGE, thermal_srmc, zone_prices
    p = zone_prices({"gas": 41.0, "coal": 15.0, "co2": 70.0, "oil": 80.0}, "GB", 2025)
    assert thermal_srmc("gas", gc._EXT_IMPORT_EFF, p) > thermal_srmc("gas", EFF_RANGE["gas"][0], p)


def test_none_of_this_touches_another_zone():
    """`apply` is an overlay on GB alone — a trajectory leaking into FR or DE_LU would be catastrophic and
    entirely invisible in any GB-focused check."""
    fr = {"nuclear": 61000.0, "coal": 3000.0}
    assert gc.apply("FR", 2040, fr) == fr
    assert gc.apply("DE_LU", 2040, fr) == fr
    got = gc.apply("GB", 2040, {"nuclear": 9229.0, "coal": 8280.0, "gas": 36149.0})
    assert got["nuclear"] != 9229.0 and got["coal"] == pytest.approx(0.0)
    assert got["gas"] == 36149.0, "gas is not in the trajectory and must pass through untouched"


def test_it_can_be_switched_off_entirely(monkeypatch):
    """Repo policy: new behaviour is opt-out at call time, never a constant baked in at import."""
    monkeypatch.setenv("DISPATCH_GB_CAPACITY", "0")
    snap = {"nuclear": 9229.0, "coal": 8280.0}
    assert gc.apply("GB", 2040, snap) == snap
    monkeypatch.setenv("DISPATCH_GB_EXT_IMPORT", "0")
    assert gc.ext_import_mw(2025) == 0.0
