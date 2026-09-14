"""Projection-mode input scaling (capacity adequacy, growth loading)."""
from __future__ import annotations

import pandas as pd
import pytest
from dispatch_model.rolling.projection import _GROWTH, _scale_stack


def _stack():
    return pd.DataFrame({
        "unit_id": ["c1", "l1", "g1", "g2", "n1"],
        "tech": ["coal", "lignite", "gas", "gas", "nuclear"],
        "capacity_mw": [5000.0, 5000.0, 4000.0, 4000.0, 6000.0],
    })


def test_retired_coal_and_lignite_is_replaced_by_gas_preserving_firm_capacity():
    st = _stack()
    firm0 = st.loc[st["tech"].isin(["coal", "lignite", "gas"]), "capacity_mw"].sum()
    g = dict(_GROWTH, coal=-0.08, lignite=-0.08)
    out = _scale_stack(st, k=21, g=g)                          # ~2040

    coal = out.loc[out["tech"].isin(["coal", "lignite"]), "capacity_mw"].sum()
    orig_coal = st.loc[st["tech"].isin(["coal", "lignite"]), "capacity_mw"].sum()
    assert coal < 0.2 * orig_coal                             # ~83 % retired
    # the retired MW landed in gas → total dispatchable-thermal firm capacity is preserved (adequacy)
    firm1 = out.loc[out["tech"].isin(["coal", "lignite", "gas"]), "capacity_mw"].sum()
    assert firm1 == pytest_approx(firm0)
    assert out.loc[out["tech"] == "gas", "capacity_mw"].sum() > st.loc[st["tech"] == "gas", "capacity_mw"].sum()


def test_no_growth_is_identity():
    st = _stack()
    out = _scale_stack(st, k=0, g=dict(_GROWTH))
    pd.testing.assert_frame_equal(out, st)


def pytest_approx(x, rel=1e-6):
    import pytest
    return pytest.approx(x, rel=rel)


# --- DISPATCH_DEMAND_SCALE (sensitivity lever) ---------------------------------------------------------

def test_demand_scale_parses_per_zone(monkeypatch):
    from dispatch_model.rolling.projection import _demand_scale
    assert _demand_scale("FR") == 1.0                            # unset → no behaviour change
    monkeypatch.setenv("DISPATCH_DEMAND_SCALE", "FR=0.90,BE=1.05")
    assert _demand_scale("FR") == 0.90
    assert _demand_scale("fr") == 0.90                           # zone match is case-insensitive
    assert _demand_scale("BE") == 1.05
    assert _demand_scale("DE_LU") == 1.0                         # a zone the list does not name
    monkeypatch.setenv("DISPATCH_DEMAND_SCALE", "")
    assert _demand_scale("FR") == 1.0


def test_demand_scale_is_read_per_call(monkeypatch):
    from dispatch_model.rolling.projection import _demand_scale
    monkeypatch.setenv("DISPATCH_DEMAND_SCALE", "FR=0.8")
    assert _demand_scale("FR") == 0.8
    monkeypatch.delenv("DISPATCH_DEMAND_SCALE")                  # never cached at import
    assert _demand_scale("FR") == 1.0


def test_demand_scale_path_interpolates_between_anchors(monkeypatch):
    from dispatch_model.rolling.projection import _demand_scale
    monkeypatch.setenv("DISPATCH_DEMAND_SCALE", "FR=2027:1.0|2037:0.95|2046:0.90")
    assert _demand_scale("FR", 2027) == pytest.approx(1.0)       # a level path starts where it starts
    assert _demand_scale("FR", 2032) == pytest.approx(0.975)     # midway between the first two anchors
    assert _demand_scale("FR", 2037) == pytest.approx(0.95)
    assert _demand_scale("FR", 2046) == pytest.approx(0.90)


def test_demand_scale_path_is_flat_outside_its_anchors(monkeypatch):
    # never EXTRAPOLATED: a ramp continued past its last anchor would keep cutting for as long as the
    # horizon runs, which is not what the variant was calibrated to say
    from dispatch_model.rolling.projection import _demand_scale
    monkeypatch.setenv("DISPATCH_DEMAND_SCALE", "FR=2030:1.0|2040:0.90")
    assert _demand_scale("FR", 2020) == pytest.approx(1.0)
    assert _demand_scale("FR", 2050) == pytest.approx(0.90)
    assert _demand_scale("FR", None) == pytest.approx(0.90)      # no year in hand -> the end-of-period ratio


def test_demand_scale_path_and_flat_coexist_and_other_zones_are_untouched(monkeypatch):
    from dispatch_model.rolling.projection import _demand_scale
    monkeypatch.setenv("DISPATCH_DEMAND_SCALE", "FR=2027:1.0|2046:0.94,BE=1.05")
    assert _demand_scale("BE", 2035) == pytest.approx(1.05)      # flat entry still parses beside a path
    assert _demand_scale("DE_LU", 2035) == pytest.approx(1.0)    # a zone the list does not name
    assert _demand_scale("FR", 2046) == pytest.approx(0.94)
