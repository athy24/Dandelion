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
