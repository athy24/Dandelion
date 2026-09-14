"""wind100 co-generation: fit recovers the conditional structure; append is coherent + reproducible."""
from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr
from weathergen.wind100 import VAR_NAME, fit_wind100


def _panels(n=24 * 400, S=6, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2018-01-01", periods=n, freq="h", tz="UTC")
    w10 = pd.DataFrame({f"S{j}": np.abs(rng.weibull(2.0, n) * 4.0) + 0.3 for j in range(S)}, index=idx)
    # w100 = 1.5 · w10^0.9 · exp(AR(1) residual shared across stations)
    resid = np.zeros((n, S)); e = rng.normal(0, 0.3, (n, S))
    for t in range(1, n):
        resid[t] = 0.85 * resid[t - 1] + e[t]
    w100 = 1.5 * w10.to_numpy() ** 0.9 * np.exp(resid)
    return w10, pd.DataFrame(w100, index=idx, columns=w10.columns)


def test_fit_recovers_structure():
    w10, w100 = _panels()
    m = fit_wind100(w10, w100)
    assert 0.7 < m.b.mean() < 1.1                       # recovers the ~0.9 log-slope
    assert 0.7 < m.phi.mean() < 0.95                    # recovers the ~0.85 residual persistence
    assert m.sigma.mean() > 0.2                         # substantial conditional variance retained


def _cube(w10: pd.DataFrame, temp: pd.DataFrame | None = None) -> xr.DataArray:
    variables, layers = ["wind_speed_ms"], [w10.to_numpy()]
    if temp is not None:
        variables.append("temperature_c"); layers.append(temp.to_numpy())
    arr = np.stack(layers, axis=-1)
    return xr.DataArray(arr, dims=("time", "station", "variable"),
                        coords={"time": w10.index, "station": list(w10.columns), "variable": variables})


def test_temperature_term_deseasonalized_recovered_and_applied():
    # temperature with a STRONG seasonal cycle + within-season noise; w100 couples to the within-season
    # anomaly (the noise), c>0 (mild winter day = Atlantic storm = windy). The fit must deseasonalize to
    # recover it — a raw-temperature fit would be confounded by the seasonal cycle.
    w10, w100base = _panels()
    rng = np.random.default_rng(5)
    n, S = w10.shape
    doy = w10.index.dayofyear.to_numpy(float)
    seasonal = 12 + 10 * np.sin(2 * np.pi * doy / 365.25)          # ±10 °C annual swing
    noise = rng.normal(0, 3, (n, S))                               # within-season variability
    temp = pd.DataFrame(seasonal[:, None] + noise, index=w10.index, columns=w10.columns)
    c_true = 0.3
    w100 = w100base * np.exp(c_true * (noise / noise.std()))
    m = fit_wind100(w10, w100, temp_panel=temp)
    assert m.c.mean() > 0.2 and abs(m.c.mean() - c_true) < 0.1     # positive, recovered despite the season cycle
    # append reproduces the within-season coupling
    out = m.append(_cube(w10, temp), np.random.default_rng(1))
    w100v = np.nanmean(out.sel(variable=VAR_NAME).values, 1)
    assert np.corrcoef((noise / noise.std()).mean(1), w100v)[0, 1] > 0.15
    # graceful fallback when the cube has no temperature
    out2 = m.append(_cube(w10), np.random.default_rng(1))
    assert VAR_NAME in set(map(str, out2["variable"].values))


def test_append_coherent_and_reproducible():
    w10, w100 = _panels()
    m = fit_wind100(w10, w100)
    cube = _cube(w10)
    a = m.append(cube, np.random.default_rng(1))
    b = m.append(cube, np.random.default_rng(1))
    c = m.append(cube, np.random.default_rng(2))
    va = a.sel(variable=VAR_NAME).values
    assert VAR_NAME in set(map(str, a["variable"].values))
    assert np.allclose(va, b.sel(variable=VAR_NAME).values)              # same seed → identical
    assert not np.allclose(va, c.sel(variable=VAR_NAME).values)          # different seed → different
    # 100 m exceeds 10 m (shear) and stays positively coherent with the 10 m field
    w10v = a.sel(variable="wind_speed_ms").values
    assert np.nanmean(va) > 1.2 * np.nanmean(w10v)
    corr = np.corrcoef(np.nanmean(va, 1), np.nanmean(w10v, 1))[0, 1]
    assert corr > 0.4
    # residual persistence carried into the output (realistic ramps)
    s = np.nanmean(va, 1) - np.nanmean(va)
    assert np.corrcoef(s[:-1], s[1:])[0, 1] > 0.4       # residual AR carried through (diluted by white w10)


# ---- diurnal shear (2026-09-14) -------------------------------------------------------------------------------
from weathergen.wind100 import DEFAULT_SPEC, Wind100Model, recalibrate  # noqa: E402


def _phase_hour(values, index) -> float:
    s = pd.Series(np.asarray(values, float), index=index)
    p = s.groupby(s.index.hour).mean()
    q = p.to_numpy() - p.mean()
    k = np.arange(24)
    return float((np.degrees(np.arctan2((q * np.sin(2 * np.pi * k / 24)).sum(),
                                        (q * np.cos(2 * np.pi * k / 24)).sum())) / 15.0) % 24)


def _dphase(a, b) -> float:
    return abs(((a - b + 12) % 24) - 12)


def _diurnal_panels(n=24 * 730, S=5, seed=3):
    """10 m wind peaks mid-afternoon (daytime mixing); 100 m wind carries an extra stability-driven shear cycle
    peaking in the evening. A transfer with no hour term can only hand the 10 m phase to the 100 m field."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2018-01-01", periods=n, freq="h", tz="UTC")
    h = idx.hour.to_numpy(float)
    day = 1.0 + 0.35 * np.cos(2 * np.pi * (h - 14) / 24)
    w10 = pd.DataFrame({f"S{j}": (np.abs(rng.weibull(2.0, n) * 3.0) + 0.3) * day for j in range(S)}, index=idx)
    resid = np.zeros((n, S)); e = rng.normal(0, 0.25, (n, S))
    for t in range(1, n):
        resid[t] = 0.85 * resid[t - 1] + e[t]
    shear = 0.45 * np.cos(2 * np.pi * (h - 21) / 24)
    w100 = np.exp(0.4 + 0.6 * np.log(w10.to_numpy() + 0.1) + shear[:, None] + resid) - 0.1
    return w10, pd.DataFrame(w100, index=idx, columns=w10.columns)


def _lst_cube(w10: pd.DataFrame) -> xr.DataArray:
    return _cube(w10).assign_coords(lst_offset_h=("station", np.zeros(w10.shape[1])))


def _nat(out) -> np.ndarray:
    return np.nanmean(out.sel(variable=VAR_NAME).transpose("time", "station").values, axis=1)


def test_the_100m_diurnal_phase_is_its_own_not_the_10m_one():
    w10, w100 = _diurnal_panels()
    offs = dict.fromkeys(w10.columns, 0.0)
    truth = _phase_hour(w100.mean(axis=1), w10.index)
    assert _dphase(truth, _phase_hour(w10.mean(axis=1), w10.index)) > 3.0      # the premise: two distinct cycles
    legacy = fit_wind100(w10, w100).append(_lst_cube(w10), np.random.default_rng(1))
    diurnal = fit_wind100(w10, w100, lst_offset_h=offs, spec=DEFAULT_SPEC).append(_lst_cube(w10), np.random.default_rng(1))
    assert _dphase(_phase_hour(_nat(diurnal), w10.index), truth) < 1.0
    assert _dphase(_phase_hour(_nat(legacy), w10.index), truth) > 2.0          # the 2026-09 defect, reproduced


def test_recalibration_corrects_a_damped_generator():
    """The generator's 10 m diurnal cycle is weaker than the observed one the transfer was fitted on; the
    national recalibration must bring the co-generated 100 m climatology back onto the target."""
    w10, w100 = _diurnal_panels()
    m = fit_wind100(w10, w100, lst_offset_h=dict.fromkeys(w10.columns, 0.0), spec=DEFAULT_SPEC)
    h = w10.index.hour.to_numpy(float)
    day = 1.0 + 0.35 * np.cos(2 * np.pi * (h - 14) / 24)
    weak = 1.0 + 0.10 * np.cos(2 * np.pi * (h - 12) / 24)
    damped = pd.DataFrame(w10.to_numpy() / day[:, None] * weak[:, None], index=w10.index, columns=w10.columns)
    target = w100.mean(axis=1)
    truth = _phase_hour(target, w10.index)
    before = _dphase(_phase_hour(_nat(m.append(_lst_cube(damped), np.random.default_rng(4))), w10.index), truth)
    recalibrate(m, _lst_cube(damped), target, np.random.default_rng(2))
    after_out = m.append(_lst_cube(damped), np.random.default_rng(4))
    after = _dphase(_phase_hour(_nat(after_out), w10.index), truth)
    assert after <= before + 1e-9
    assert after < 0.75
    assert 0.95 < float(np.nanmean(_nat(after_out)) / target.mean()) < 1.05


def test_diurnal_model_round_trips_through_save(tmp_path):
    w10, w100 = _diurnal_panels(n=24 * 200)
    m = fit_wind100(w10, w100, lst_offset_h=dict.fromkeys(w10.columns, 0.1), spec=DEFAULT_SPEC)
    m.recal = np.linspace(-0.01, 0.01, 35)
    m2 = Wind100Model.load(m.save(tmp_path / "w.json"))
    a = m.append(_lst_cube(w10), np.random.default_rng(9)).sel(variable=VAR_NAME).values
    b = m2.append(_lst_cube(w10), np.random.default_rng(9)).sel(variable=VAR_NAME).values
    assert np.allclose(a, b)


# ---- residual coherence, generator guard, legacy closed form (2026-09-14, after review) ------------------------
import pytest  # noqa: E402
from scipy.signal import lfilter  # noqa: E402
from weathergen.wind100 import generator_identity  # noqa: E402

from weathergen import simulate as _simmod  # noqa: E402


def _flat_model(S=6, sigma=0.4, phi=0.8):
    return Wind100Model(station_ids=[f"S{j}" for j in range(S)], a=np.full(S, 0.5), b=np.full(S, 0.6),
                        phi=np.full(S, phi), sigma=np.full(S, sigma), chol=np.eye(S), fit_r2=np.zeros(S))


def _const_cube(n=24 * 3000, S=6, value=5.0):
    idx = pd.date_range("2018-01-01", periods=n, freq="h", tz="UTC")
    return _cube(pd.DataFrame(np.full((n, S), value), index=idx, columns=[f"S{j}" for j in range(S)]))


def test_legacy_append_matches_its_closed_form_and_draws_exactly_one_block():
    n, S = 24 * 50, 6
    rng = np.random.default_rng(42)
    out = _flat_model().append(_const_cube(n=n), rng).sel(variable=VAR_NAME).transpose("time", "station").values
    ref = np.random.default_rng(42)
    innov = ref.standard_normal((n, S)) * (0.4 * np.sqrt(1 - 0.8 ** 2))
    resid = np.column_stack([lfilter([1.0], [1.0, -0.8], innov[:, j]) for j in range(S)])
    expected = np.clip(np.exp(0.5 + 0.6 * np.log(5.0 + 0.1) + resid) - 0.1, 0, None)
    np.testing.assert_allclose(out, expected, rtol=1e-12, atol=0)
    assert rng.bit_generator.state == ref.bit_generator.state                  # nothing else drawn


def test_common_factor_keeps_station_variance_and_sets_coherence():
    lam = 0.3
    out = (_flat_model().with_residual(common_share=lam, common_phi=0.97)
           .append(_const_cube(), np.random.default_rng(7)).sel(variable=VAR_NAME).transpose("time", "station").values)
    lg = np.log(out + 0.1)
    assert np.allclose(lg.var(axis=0), 0.4 ** 2, rtol=0.15)                   # a station's variance is unchanged
    off = np.corrcoef(lg.T)[np.triu_indices(6, 1)].mean()
    assert abs(off - lam) < 0.07                                               # lambda + (1 - lambda) * R, R = I


def test_the_common_factor_draws_after_the_station_block():
    n, S = 24 * 20, 6
    base = _flat_model()
    cube = _const_cube(n=n)
    r1, r2 = np.random.default_rng(3), np.random.default_rng(3)
    base.append(cube, r1)
    base.with_residual(0.2, 0.95).append(cube, r2)
    ref = np.random.default_rng(3)
    ref.standard_normal((n, S))
    assert r1.bit_generator.state == ref.bit_generator.state                   # without it: the legacy stream
    ref.standard_normal()
    ref.standard_normal(n)
    assert r2.bit_generator.state == ref.bit_generator.state                   # with it: one scalar + n more


def test_with_residual_is_idempotent():
    a = _flat_model().with_residual(0.2, 0.9, sigma_scale=0.9)
    b = a.with_residual(0.2, 0.9, sigma_scale=0.9)
    assert np.allclose(a.sigma, 0.36) and np.allclose(b.sigma, 0.36)
    assert a.with_residual(None, None).common_share is None


def test_unknown_stations_under_a_spec_model_follow_their_own_local_solar_time():
    w10, w100 = _diurnal_panels(n=24 * 400, S=4)
    m = fit_wind100(w10, w100, lst_offset_h=dict.fromkeys(w10.columns, 0.0), spec=DEFAULT_SPEC)
    m.sigma = np.full(len(m.sigma), 1e-9)                                      # isolate the deterministic transfer
    flat = pd.DataFrame({"X_east": np.full(len(w10), 5.0), "X_west": np.full(len(w10), 5.0)}, index=w10.index)
    cube = _cube(pd.concat([w10, flat], axis=1)).assign_coords(
        lst_offset_h=("station", np.array([0.0, 0.0, 0.0, 0.0, 3.0, -3.0])),
        latitude=("station", np.array([46.0, 47.0, 45.0, 48.0, 46.5, 46.5])),
        longitude=("station", np.array([2.0, 3.0, 1.0, 4.0, 45.0, -45.0])))
    vals = m.append(cube, np.random.default_rng(0)).sel(variable=VAR_NAME).transpose("time", "station").values
    assert np.isfinite(vals).all()
    ph_east = _phase_hour(np.log(vals[:, 4] + 0.1), w10.index)
    ph_west = _phase_hour(np.log(vals[:, 5] + 0.1), w10.index)
    assert abs((((ph_west - ph_east + 12) % 24) - 12) - 6.0) < 0.3                    # same LST peak, 6 h apart in UTC


def test_recalibrate_does_not_compound_on_a_previous_recalibration():
    w10, w100 = _diurnal_panels(n=24 * 400)
    m = fit_wind100(w10, w100, lst_offset_h=dict.fromkeys(w10.columns, 0.0), spec=DEFAULT_SPEC)
    c1 = recalibrate(m, _lst_cube(w10), w100.mean(axis=1), np.random.default_rng(5)).copy()
    c2 = recalibrate(m, _lst_cube(w10), w100.mean(axis=1), np.random.default_rng(5))
    assert np.allclose(c1, c2)


class _Cfg:
    def __init__(self, root):
        self.path = root / "config.yaml"
        self.models_dir = root / "models"

    def section(self, name):
        return {"wind100_model": "models/wind100.json"}


def test_simulate_refuses_a_recalibration_estimated_for_another_generator(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "fitted.json").write_text("{}", encoding="utf-8")
    w10, w100 = _diurnal_panels(n=24 * 100)
    m = fit_wind100(w10, w100, lst_offset_h=dict.fromkeys(w10.columns, 0.0), spec=DEFAULT_SPEC)
    m.recal = np.zeros(35)
    m.recal_generator = "not-this-generator"
    m.save(tmp_path / "models" / "wind100.json")
    with pytest.raises(RuntimeError, match="recalibration was estimated for"):
        _simmod._append_wind100(_lst_cube(w10), _Cfg(tmp_path), np.random.default_rng(0))
    m.recal_generator = generator_identity(tmp_path / "models")
    m.save(tmp_path / "models" / "wind100.json")
    out = _simmod._append_wind100(_lst_cube(w10), _Cfg(tmp_path), np.random.default_rng(0))
    assert VAR_NAME in set(map(str, out["variable"].values))


def test_the_generator_guard_is_silent_when_the_model_applies_to_no_station(tmp_path):
    """A cube holding none of the fitted stations (the synthetic smoke test) is returned untouched by `append`, so a
    recalibration stamped for another generator has nothing to misapply and must not stop the simulation."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "fitted.json").write_text("{}", encoding="utf-8")
    w10, w100 = _diurnal_panels(n=24 * 100)
    m = fit_wind100(w10, w100, lst_offset_h=dict.fromkeys(w10.columns, 0.0), spec=DEFAULT_SPEC)
    m.recal = np.zeros(35)
    m.recal_generator = "not-this-generator"
    m.save(tmp_path / "models" / "wind100.json")
    synthetic = w10.rename(columns=lambda s: f"SYNTH_{s}")
    cube = _cube(synthetic).assign_coords(lst_offset_h=("station", np.zeros(synthetic.shape[1])))
    out = _simmod._append_wind100(cube, _Cfg(tmp_path), np.random.default_rng(0))
    assert VAR_NAME not in set(map(str, out["variable"].values))
