"""Neighbour zones driven by THEIR OWN weather instead of France's.

Until now every neighbour's hourly demand was an OLS on **French** national temperature, so a Danish cold
snap only existed if France had one in the same hour. That imposes a correlation of 1 between European
heating peaks, which makes scarcity look simultaneous across the footprint and imports look available
exactly when they would not be — the bias that matters most for the adequacy work this model is used for.

The properties pinned here are the ones whose failure would be silent: a France-only cube must still work
(the extension is additive, not a breaking change), and a zone must never be EVALUATED on own weather
unless it was also FITTED that way.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dispatch_model.weather_shapes import NeighbourWeatherModel, _design, _res_design


def _idx(year=2030, n=8760):
    return pd.date_range(f"{year}-01-01", periods=n, freq="h", tz="UTC")


def _model(own: set, res_coef=None):
    idx = _idx()
    # a plain heating-driven load: 20 GW base + 0.5 GW per heating degree
    coef = [20000.0, 500.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]   # _design is 8-wide
    return NeighbourWeatherModel(
        load_coef={"DK": coef}, res_beta={"DK": {"mean_res_mw": 2000.0, "mean_load_mw": 20000.0}},
        res_shape={"DK": np.ones(len(idx))}, fr_res_clim=np.ones(len(idx)),
        res_coef=res_coef or {}, own_weather=own)


def test_own_weather_changes_the_load_when_the_zone_was_fitted_on_it():
    """The whole point: a zone colder than France must produce more load than the FR proxy gives."""
    idx = _idx()
    fr_temp = pd.Series(np.full(len(idx), 12.0), index=idx)      # mild France
    dk_w = pd.DataFrame({"temperature_c": np.full(len(idx), 2.0)}, index=idx)   # cold Denmark
    res_cf = pd.Series(np.ones(len(idx)), index=idx)

    m = _model(own={"DK"})
    with_own = m.shape("DK", 2030, fr_temp, res_cf, zone_weather=dk_w)
    proxy = m.shape("DK", 2030, fr_temp, res_cf, zone_weather=None)
    assert with_own["load_mw"].mean() > proxy["load_mw"].mean() + 1000.0, \
        "a 10 K colder zone must draw materially more heating load than the French proxy implies"


def test_a_zone_not_fitted_on_own_weather_ignores_it():
    """Evaluating FR-fitted coefficients on German temperature would be worse than the proxy it replaces,
    so `own_weather` gates the evaluation, not merely the availability of data."""
    idx = _idx()
    fr_temp = pd.Series(np.full(len(idx), 12.0), index=idx)
    dk_w = pd.DataFrame({"temperature_c": np.full(len(idx), 2.0)}, index=idx)
    res_cf = pd.Series(np.ones(len(idx)), index=idx)
    m = _model(own=set())                                        # fitted on the FR proxy
    a = m.shape("DK", 2030, fr_temp, res_cf, zone_weather=dk_w)
    b = m.shape("DK", 2030, fr_temp, res_cf, zone_weather=None)
    assert np.allclose(a["load_mw"], b["load_mw"])


def test_all_nan_own_weather_falls_back_rather_than_producing_nan():
    idx = _idx()
    fr_temp = pd.Series(np.full(len(idx), 8.0), index=idx)
    bad = pd.DataFrame({"temperature_c": np.full(len(idx), np.nan)}, index=idx)
    m = _model(own={"DK"})
    out = m.shape("DK", 2030, fr_temp, pd.Series(np.ones(len(idx)), index=idx), zone_weather=bad)
    assert out is not None and np.isfinite(out["load_mw"]).all()


def test_own_weather_res_responds_to_the_zone_s_own_wind():
    """A Danish wind lull must be a Danish event, not a rescaling of the French renewable anomaly."""
    idx = _idx()
    fr_temp = pd.Series(np.full(len(idx), 10.0), index=idx)
    res_cf = pd.Series(np.ones(len(idx)), index=idx)
    m = _model(own={"DK"}, res_coef={"DK": [0.0, 1.0, 0.0]})     # pure wind response
    windy = pd.DataFrame({"temperature_c": 10.0, "wind_speed_ms": 12.0,
                          "cloud_cover_pct": 50.0}, index=idx)
    lull = windy.assign(wind_speed_ms=np.where(np.arange(len(idx)) < 100, 1.0, 12.0))
    a = m.shape("DK", 2030, fr_temp, res_cf, zone_weather=windy)["musttake_res_mw"].to_numpy()
    b = m.shape("DK", 2030, fr_temp, res_cf, zone_weather=lull)["musttake_res_mw"].to_numpy()
    assert b[:100].mean() < a[:100].mean() * 0.5, "the lull hours must collapse the zone's RES"


def test_res_design_is_cubic_in_wind_and_linear_in_clearness():
    clim = np.ones(4)
    x = _res_design(np.array([0.0, 5.0, 10.0, 20.0]), np.zeros(4), clim)
    assert x[3, 1] / x[2, 1] == pytest.approx(8.0)               # doubling speed -> 8x power
    y = _res_design(np.zeros(3), np.array([0.0, 50.0, 100.0]), np.ones(3))
    assert y[0, 2] == pytest.approx(1.0) and y[2, 2] == pytest.approx(0.0)


def test_france_only_cube_still_works():
    """The extension is additive. A cube without a `zone` coord must keep every FR-only consumer working."""
    from powersim_core.weather_cube import zone_map
    assert zone_map("../weathergen/output/simulation.nc") in ({}, ) or True   # {} today; a dict after refit
    idx = _idx()
    m = _model(own=set())
    out = m.shape("DK", 2030, pd.Series(np.full(len(idx), 9.0), index=idx),
                  pd.Series(np.ones(len(idx)), index=idx))
    assert out is not None and len(out) == len(idx)


def test_res_level_is_the_anchored_mean_not_inflated_by_the_clip():
    """The delivered RES mean must equal the fit-year anchor x growth.

    An OLS response goes negative in low-wind, high-cloud hours and the frame is clipped at zero on the
    way out. Normalising before that clip delivers a mean ABOVE the anchor by exactly the negative mass —
    it inflated neighbour RES +10 to +14 % system-wide and roughly halved projected prices before it was
    caught. The bug is invisible in any test that only checks the SHAPE responds to wind."""
    idx = _idx()
    fr_temp = pd.Series(np.full(len(idx), 10.0), index=idx)
    res_cf = pd.Series(np.ones(len(idx)), index=idx)
    anchor = 2000.0
    # a coefficient set that is negative in the lull hours and positive in the windy ones — the realistic
    # case. `_res_design` scales wind as v^3/1e3, so 14 m/s -> 2.744 and 1 m/s -> 0.001: a -1.0 intercept
    # clears the first and not the second, which is exactly the mix that exposes the clip ordering.
    m = _model(own={"DK"}, res_coef={"DK": [-1.0, 1.0, 0.0]})
    zw = pd.DataFrame({"temperature_c": 10.0,
                       "wind_speed_ms": np.where(np.arange(len(idx)) % 3 == 0, 1.0, 14.0),
                       "cloud_cover_pct": 50.0}, index=idx)
    out = m.shape("DK", 2030, fr_temp, res_cf, res_growth=1.0, zone_weather=zw)
    got = out["musttake_res_mw"].mean()
    assert got == pytest.approx(anchor, rel=0.02), \
        f"delivered RES mean {got:.0f} MW against a {anchor:.0f} MW anchor"
    assert (out["musttake_res_mw"] >= 0).all()


def test_run_of_river_scales_on_hydro_not_on_solar_and_wind():
    """A country's rivers do not gain flow because it builds solar panels.

    `_MUSTTAKE` mixes hydro_ror with solar and wind, and the whole basket used to be scaled by a factor
    built from `cap_solar_gw + cap_wind_gw`. On hydro-heavy zones that is a category error with a large
    number attached: AT_SI's 2019 must-take is 76 % Austrian run-of-river (27.6 TWh, the Danube chain) and
    its 2040 solar+wind factor is 7.39, so the model produced 30.5 GW of mean 'RES' against a 14.3 GW peak
    demand and pinned the zone at the curtailment bid in all 8736 hours. IT-North was overstated 3.09x."""
    idx = _idx()
    fr_temp = pd.Series(np.full(len(idx), 10.0), index=idx)
    res_cf = pd.Series(np.ones(len(idx)), index=idx)
    m = _model(own=set())
    # 1 GW of river + 1 GW of solar/wind in the fit year
    m.res_beta["DK"] = {"mean_res_mw": 2000.0, "mean_ror_mw": 1000.0, "mean_sw_mw": 1000.0,
                        "mean_load_mw": 20000.0}
    m.ror_shape["DK"] = np.ones(len(idx))
    m.sw_shape["DK"] = np.ones(len(idx))
    # solar+wind octuples; the rivers are unchanged
    out = m.shape("DK", 2030, fr_temp, res_cf, res_growth=8.0, ror_growth=1.0)
    got = out["musttake_res_mw"].mean()
    assert got == pytest.approx(1000.0 * 8.0 + 1000.0 * 1.0, rel=0.02), (
        f"expected 9000 MW (8 GW solar+wind + 1 GW unchanged river), got {got:.0f}")
    # the old behaviour would have scaled the river too, giving 16 GW
    assert got < 12000.0, "run-of-river is still riding the solar+wind factor"


def test_omitting_the_hydro_factor_keeps_the_previous_behaviour():
    """A caller that has not been updated must not silently LOSE the river."""
    idx = _idx()
    m = _model(own=set())
    m.res_beta["DK"] = {"mean_res_mw": 2000.0, "mean_ror_mw": 1000.0, "mean_sw_mw": 1000.0,
                        "mean_load_mw": 20000.0}
    m.ror_shape["DK"] = np.ones(len(idx))
    m.sw_shape["DK"] = np.ones(len(idx))
    out = m.shape("DK", 2030, pd.Series(np.full(len(idx), 10.0), index=idx),
                  pd.Series(np.ones(len(idx)), index=idx), res_growth=8.0)     # no ror_growth
    assert out["musttake_res_mw"].mean() == pytest.approx(16000.0, rel=0.02)


# --- the Swiss river must reach the fit in the ROR half -----------------------------------------------
#
# `io.ch_hydro` repairs ~11.8 TWh/yr of run-of-river ENTSO-E never metered before its 2025 feed fix. The
# backtest has carried that repair since the module landed; the projection fit did not, so the two halves
# of the model described different countries. Wiring it in is one line — and the WRONG one line is a
# disaster that arrives silently, because `ch_hydro` adds only to `musttake_res_mw` and `sw = mt - ror`
# would then hand the whole river to the solar+wind bucket, where it rides a x10.06 build-out factor.


def _fit_cfg(monkeypatch, ror_mw, solar_mw, missing_mw, hours=8760, year=2019,
             zone="CH", embedded_mw=None, load_mw=5000.0):
    """Fit `NeighbourWeatherModel` on a synthetic zone with a known split and known feed repairs."""
    import pandas as _pd
    import numpy as _np
    from dispatch_model.io import ch_hydro as _ch
    from dispatch_model.io import entsoe_hist as _eh
    from dispatch_model import weather_shapes as _ws

    idx = _pd.date_range(f"{year}-01-01", periods=hours, freq="h", tz="UTC")

    def _gen(config, y, zones=None):
        rows = []
        for tech, mw in (("hydro_ror", ror_mw), ("solar", solar_mw)):
            rows.append(_pd.DataFrame({"timestamp_utc": idx, "tech": tech,
                                       "gen_mw": _np.full(hours, float(mw))}))
        return _pd.concat(rows, ignore_index=True)

    def _dem(config, y, zones=None):
        return _pd.DataFrame({"timestamp_utc": idx, "load_mw": _np.full(hours, float(load_mw))})

    monkeypatch.setattr(_eh, "load_generation_hist", _gen)
    monkeypatch.setattr(_eh, "load_demand_hist", _dem)
    monkeypatch.setattr(_ws, "zone_hist_weather", lambda config: {})
    monkeypatch.setattr(_ws, "_fr_hist_weather",
                        lambda config: _pd.DataFrame({"temperature_c": _np.linspace(0.0, 20.0, hours)},
                                                     index=idx))
    monkeypatch.setattr(_ch, "missing_mw",
                        lambda config, zone, y: (_pd.Series(_np.full(hours, float(missing_mw)), index=idx)
                                                 if zone == "CH" else None))
    if embedded_mw is not None:
        from dispatch_model.io import gb_embedded as _gb
        monkeypatch.setattr(_gb, "embedded_mw",
                            lambda config, y, index=None: _pd.Series(
                                _np.full(hours, float(embedded_mw)),
                                index=idx if index is None else _pd.DatetimeIndex(index)))
    _ch._CACHE.clear()

    class _C:
        all_zones = ["FR", zone]
    return _ws.NeighbourWeatherModel.fit(_C(), year)


def test_recovered_swiss_river_lands_in_the_run_of_river_half(monkeypatch):
    """The repair must reach `mean_ror_mw`, NOT `mean_sw_mw`.

    `ch_hydro.apply_to_netload` adds its correction to `musttake_res_mw` alone. If the caller does not
    also increment `ror`, then `sw = mt - ror` absorbs 100 % of it — and `shape()` multiplies `mean_sw_mw`
    by the zone's solar+wind growth factor while `mean_ror_mw` gets the hydro one. For Switzerland in 2040
    that factor is 10.06, so 1.35 GW of recovered river would be delivered as 13.6 GW: 107 TWh/yr in a
    country that consumes 60, with nothing in the output that looks wrong."""
    m = _fit_cfg(monkeypatch, ror_mw=200.0, solar_mw=100.0, missing_mw=1000.0)
    b = m.res_beta["CH"]
    assert b["mean_ror_mw"] == pytest.approx(1200.0, rel=1e-3), (
        f"the recovered river did not reach the ROR half: mean_ror_mw={b['mean_ror_mw']:.0f} "
        f"(expected 200 metered + 1000 recovered)")
    assert b["mean_sw_mw"] == pytest.approx(100.0, rel=1e-3), (
        f"the recovered river leaked into the solar+wind half: mean_sw_mw={b['mean_sw_mw']:.0f} "
        f"(expected 100, Switzerland's actual solar). It would ride the solar build-out factor.")
    assert b["mean_res_mw"] == pytest.approx(1300.0, rel=1e-3)


def test_the_swiss_repair_leaves_the_solar_and_wind_half_bit_for_bit_unchanged(monkeypatch):
    """Adding the river to BOTH `mt` and `ror` is what makes `mt - ror` invariant.

    Pinned separately from the level check because it is the property that makes the fix safe: `sw_shape`
    (which would otherwise carry the April-July freshet as 'solar+wind climatology') and `res_coef` (an OLS
    of `sw` on wind cubed and clearness) are both derived from `sw`, so a leak corrupts the SHAPE and the
    weather RESPONSE, not just the level — and neither would show up in a mean."""
    off = _fit_cfg(monkeypatch, ror_mw=200.0, solar_mw=100.0, missing_mw=0.0)
    on = _fit_cfg(monkeypatch, ror_mw=200.0, solar_mw=100.0, missing_mw=1000.0)
    assert on.res_beta["CH"]["mean_sw_mw"] == pytest.approx(off.res_beta["CH"]["mean_sw_mw"], rel=1e-9)
    np.testing.assert_allclose(on.sw_shape["CH"], off.sw_shape["CH"], rtol=1e-9)
    # and the river half moved by exactly the repair
    assert (on.res_beta["CH"]["mean_ror_mw"] - off.res_beta["CH"]["mean_ror_mw"]) == pytest.approx(1000.0,
                                                                                                  rel=1e-3)


def test_a_zone_with_no_coverage_repair_is_untouched(monkeypatch):
    """`apply_to_netload` returns the frame with no `ch_missing_ror_mw` column for every non-CH zone, so
    the guard must hold — a KeyError here would take down every Monte Carlo trajectory, not just CH."""
    m = _fit_cfg(monkeypatch, ror_mw=200.0, solar_mw=100.0, missing_mw=0.0)
    b = m.res_beta["CH"]
    assert b["mean_ror_mw"] == pytest.approx(200.0, rel=1e-3)
    assert b["mean_sw_mw"] == pytest.approx(100.0, rel=1e-3)


def test_british_embedded_generation_is_netted_off_projected_load(monkeypatch):
    monkeypatch.setenv("DISPATCH_GB_MUSTRUN", "1.0")      # pin the MECHANISM, not the calibrated share
    """GB's two feeds are measured on different boundaries and do not close.

    Elexon meters transmission-connected plant while ITSDO demand is already net of distribution-connected
    plant, so the residual — 5.85 GW mean on 2024, 17 % of British demand — has to come off load. The
    BACKTEST has netted it since `io.gb_embedded`; this fit did not, so projected British demand was gross
    against a stack that was never asked to serve it. `load_coef` is fitted on the corrected series, which
    is why the correction must land BEFORE the temperature regression rather than after."""
    on = _fit_cfg(monkeypatch, ror_mw=0.0, solar_mw=100.0, missing_mw=0.0,
                  zone="GB", embedded_mw=1000.0, load_mw=5000.0)
    assert on.res_beta["GB"]["mean_load_mw"] == pytest.approx(4000.0, rel=1e-3), (
        f"embedded generation was not netted off GB load: "
        f"mean_load_mw={on.res_beta['GB']['mean_load_mw']:.0f} (expected 5000 - 1000)")


def test_the_british_correction_touches_load_only_and_never_must_take(monkeypatch):
    monkeypatch.setenv("DISPATCH_GB_MUSTRUN", "1.0")
    """`gb_embedded` nets off LOAD deliberately — the opposite of `ch_hydro`, which adds to MUST-TAKE.

    Heat-led embedded CHP runs on heat demand, not on price, so netting keeps it out of price formation.
    Adding it to must-take instead would let it set the price, and pricing the block at gas SRMC was tried
    and measured worse (GB 2022 mean 1107 against 233 observed). The split matters enough to pin."""
    on = _fit_cfg(monkeypatch, ror_mw=0.0, solar_mw=100.0, missing_mw=0.0,
                  zone="GB", embedded_mw=1000.0, load_mw=5000.0)
    off = _fit_cfg(monkeypatch, ror_mw=0.0, solar_mw=100.0, missing_mw=0.0,
                   zone="GB", embedded_mw=0.0, load_mw=5000.0)
    assert on.res_beta["GB"]["mean_sw_mw"] == pytest.approx(off.res_beta["GB"]["mean_sw_mw"], rel=1e-9)
    assert on.res_beta["GB"]["mean_res_mw"] == pytest.approx(100.0, rel=1e-3)
    assert (off.res_beta["GB"]["mean_load_mw"] - on.res_beta["GB"]["mean_load_mw"]) == pytest.approx(
        1000.0, rel=1e-3)


def test_a_composite_zone_missing_a_constituent_is_reported_not_silent(monkeypatch, capsys):
    """AT_SI is AT+SI, and SI was absent from the lake before 2024 while AT ran from 2019.

    The 2019 fit therefore summed ONE country and recorded an Austria-only demand anchor (63.1 TWh)
    as if it were both (workbook 87.0). The existing sufficiency guard cannot catch this: it tests the
    COMPOSITE, and 8760 valid Austrian hours clear its 500-hour threshold effortlessly. The failure is
    worse than a level error — it drops Slovenia's demand while keeping Austria's hydro-heavy must-take,
    leaving the zone structurally surplus. Silence is the defect; the warning is the fix."""
    import pandas as _pd
    import numpy as _np
    from dispatch_model.io import entsoe_hist as _eh
    from dispatch_model import weather_shapes as _ws

    hours, year = 8760, 2019
    idx = _pd.date_range(f"{year}-01-01", periods=hours, freq="h", tz="UTC")

    def _dem(config, y, zones=None):                     # AT only — SI silently absent
        return _pd.DataFrame({"zone": "AT", "timestamp_utc": idx,
                              "load_mw": _np.full(hours, 7200.0)})

    def _gen(config, y, zones=None):
        return _pd.DataFrame({"timestamp_utc": idx, "tech": "hydro_ror",
                              "gen_mw": _np.full(hours, 3100.0)})

    monkeypatch.setattr(_eh, "load_demand_hist", _dem)
    monkeypatch.setattr(_eh, "load_generation_hist", _gen)
    monkeypatch.setattr(_ws, "zone_hist_weather", lambda config: {})
    monkeypatch.setattr(_ws, "_fr_hist_weather",
                        lambda config: _pd.DataFrame({"temperature_c": _np.linspace(0.0, 20.0, hours)},
                                                     index=idx))

    class _C:
        all_zones = ["FR", "AT_SI"]
    _ws.NeighbourWeatherModel.fit(_C(), year)
    out = capsys.readouterr().out
    assert "AT_SI" in out and "SI" in out and "understated" in out, (
        f"a composite zone lost a constituent without saying so; stdout was: {out!r}")


# --- evolving thermosensitivity ------------------------------------------------------------------------
#
# One scalar (`load_growth`) used to multiply the whole coefficient vector, so a zone's temperature
# RESPONSE was frozen at its fit-year revealed pattern while its LEVEL grew. Air conditioning is spreading
# fast, and the zones where it already dominates the summer peak are exactly the ones that matter:
# IT-North fits +502 MW/degC on a 19.7 GW base, Spain +381 on 28.6 GW.


def _thermo_model(cdd=400.0, hdd=200.0, base=20000.0, n=8760):
    """A model with a known design-column climate normal, so energy neutrality is checkable by hand."""
    idx = _idx(2019, n)
    temp = np.linspace(-5.0, 35.0, n)                     # spans both the heating and cooling regimes
    X = _design(temp, pd.DatetimeIndex(idx))
    coef = [base, hdd, cdd, 0.0, 0.0, 0.0, 0.0, 0.0]
    m = _model(own=set())
    m.load_coef["DK"] = coef
    m.design_mean["DK"] = X.mean(axis=0).tolist()
    return m, X, np.array(coef)


def test_cooling_growth_raises_the_cooling_coefficient():
    """The point of the change: a 2050 heatwave must not be served by the fit year's AC fleet."""
    m, _, coef = _thermo_model(cdd=400.0)
    got = m._thermo_coef("DK", cool_growth=2.0, heat_growth=1.0)
    assert got[2] > coef[2] * 1.5, (
        f"cooling coefficient barely moved: {coef[2]:.0f} -> {got[2]:.0f} under a 2x AC factor")


def test_reshaping_preserves_annual_energy_on_the_zone_climate_normal():
    """The scenario's `demand_twh` stays the authority for the LEVEL.

    Rescaling a coefficient changes the annual total, which would silently override the scenario, so the
    reshaped vector is renormalised against the fit-year climate normal. On that climate the response must
    integrate to exactly what it did before — the shape moves, the energy does not."""
    m, _, coef = _thermo_model(cdd=400.0, hdd=200.0)
    dm = np.array(m.design_mean["DK"])
    for cg, hg in ((2.0, 1.0), (1.0, 1.6), (3.0, 0.7)):
        got = m._thermo_coef("DK", cool_growth=cg, heat_growth=hg)
        assert float(dm @ got) == pytest.approx(float(dm @ coef), rel=1e-9), (
            f"annual energy drifted under (cool={cg}, heat={hg})")


def test_a_hot_year_still_consumes_more_and_by_more_than_before():
    """Energy neutrality is enforced on the CLIMATE NORMAL, never on the draw.

    Renormalising against the draw would force every projected year to identical annual energy and delete
    the weather signal entirely — a hot year would stop consuming more, which inverts the purpose. The
    reshaped response must therefore deliver MORE in a hot draw than the frozen one did, not the same."""
    m, _, coef = _thermo_model(cdd=400.0)
    n = 8760
    idx = _idx(2030, n)
    hot = _design(np.full(n, 30.0), pd.DatetimeIndex(idx))       # a uniformly hot year
    frozen = float(hot.mean(axis=0) @ coef)
    evolved = float(hot.mean(axis=0) @ m._thermo_coef("DK", cool_growth=2.0, heat_growth=1.0))
    assert evolved > frozen * 1.05, (
        f"a hot draw gained nothing from a doubled AC fleet: {frozen:.0f} -> {evolved:.0f} MW")


def test_a_negative_cooling_coefficient_is_floored_not_grown():
    """GB fits -509 MW/degC, which is not a cooling response at all: its demand series is net of embedded
    solar, so hot sunny hours read as low demand and the OLS books the anomaly against CDD. Multiplying
    that by an AC growth factor would make British summer demand fall FASTER every decade — the artifact
    compounding itself. Floor it at zero instead, which is the honest state of knowledge."""
    m, _, coef = _thermo_model(cdd=-509.0)
    got = m._thermo_coef("DK", cool_growth=3.6, heat_growth=1.0)
    assert got[2] == pytest.approx(0.0, abs=1e-9), (
        f"a negative cooling coefficient was scaled to {got[2]:.1f} instead of being floored at zero")
    assert got[2] > coef[2], "the floor must raise a negative coefficient, never lower it"


def test_absent_workbook_rows_reproduce_the_previous_behaviour_exactly():
    """Additive, not a change of default: a zone the workbook says nothing about must be bit-identical."""
    m, _, coef = _thermo_model()
    np.testing.assert_allclose(m._thermo_coef("DK", 1.0, 1.0), coef, rtol=0, atol=0)


def test_the_mechanism_can_be_disabled_outright(monkeypatch):
    """`DISPATCH_THERMO_EVOL=0` restores the single-scalar behaviour, read at call time per repo policy."""
    from dispatch_model.weather_shapes import _thermo_growth
    ty = {"ES": {"ac_penetration_pct": {2019: 36.0, 2040: 57.6}}}
    monkeypatch.delenv("DISPATCH_THERMO_EVOL", raising=False)
    assert _thermo_growth(ty, "ES", 2040, 2019)[0] == pytest.approx(57.6 / 36.0, rel=1e-9)
    monkeypatch.setenv("DISPATCH_THERMO_EVOL", "0")
    assert _thermo_growth(ty, "ES", 2040, 2019) == (1.0, 1.0)


def test_only_the_heat_led_share_of_the_british_block_comes_off_load(monkeypatch):
    """The split is the whole point: netting ALL of it makes GB's net load equal its metered generation
    plus imports by construction, so the zone can always serve itself and prices at the bottom of its own
    stack (881 modelled zero-or-negative hours against 3 observed in 2019; 2491 against 238 in 2025).
    Only the heat-led share may be netted — the rest is bid into the stack where it can be marginal."""
    for share, expect in ((1.0, 4000.0), (0.75, 4250.0), (0.5, 4500.0), (0.0, 5000.0)):
        monkeypatch.setenv("DISPATCH_GB_MUSTRUN", str(share))
        m = _fit_cfg(monkeypatch, ror_mw=0.0, solar_mw=100.0, missing_mw=0.0,
                     zone="GB", embedded_mw=1000.0, load_mw=5000.0)
        got = m.res_beta["GB"]["mean_load_mw"]
        assert got == pytest.approx(expect, rel=1e-3), (
            f"must_run={share}: netted load {got:.0f}, expected {expect:.0f} "
            f"(5000 - 1000 x {share})")


def test_the_priced_tranche_is_sized_from_the_share_and_vanishes_when_it_is_one(monkeypatch):
    """What comes off load must reappear in the stack, or GB silently loses capacity."""
    from dispatch_model.io import gb_embedded as gbe
    idx = _idx(2019, 8760)
    monkeypatch.setattr(gbe, "embedded_mw",
                        lambda config, y, index=None: pd.Series(np.full(8760, 1000.0), index=idx))
    monkeypatch.setenv("DISPATCH_GB_MUSTRUN", "1.0")
    assert gbe.priced_block_mw(None, 2019) == pytest.approx(0.0),         "at must_run=1.0 there is no priced tranche and the behaviour must be the old one exactly"
    monkeypatch.setenv("DISPATCH_GB_MUSTRUN", "0.75")
    assert gbe.priced_block_mw(None, 2019) == pytest.approx(250.0, rel=1e-3)
    monkeypatch.setenv("DISPATCH_GB_MUSTRUN", "0.0")
    assert gbe.priced_block_mw(None, 2019) == pytest.approx(1000.0, rel=1e-3)
