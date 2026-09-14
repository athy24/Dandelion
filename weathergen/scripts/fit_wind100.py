"""Fit the 100 m-wind co-generation model (conditioned on 10 m station wind).

Assembles historical panels (SYNOP 10 m from the pricemodeling DB, ERA5 100 m from the step-(iv) extract) for the
simulated cube's stations and, in one run:

  1. fits the transfer with the diurnal x seasonal shear terms (`DEFAULT_SPEC`), on ERA5 with its two
     analysis-window seams (10 and 22 UTC) interpolated out;
  2. estimates the persistence of the residual's national common mode from the slow decay of the national-mean
     residual;
  3. calibrates the residual's common share and sigma scale, GENERATOR-driven on several realizations, so the
     co-generated field matches ERA5's national (month, hour)-anomaly variance and mean station log-anomaly
     variance;
  4. recalibrates the national diurnal x seasonal log mean on the same realizations pooled;
  5. scores the result against the pre-fix model on a HELD-OUT generator realization, and saves
     `models/wind100.json`, stamped with the identity of the 10 m generator fit it belongs to.

Re-run whenever the 10 m generator (`models/fitted.json`) is refit: steps 3-4 belong to the pair, and
`simulate._append_wind100` refuses a recalibration estimated for another generator.

    python scripts/fit_wind100.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from scipy.signal import lfilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from weathergen.cli import _build_trend  # noqa: E402
from weathergen.config import load_config  # noqa: E402
from weathergen.model import FittedModel  # noqa: E402
from weathergen.wind100 import (  # noqa: E402
    _EPS,
    DEFAULT_SPEC,
    VAR_NAME,
    Wind100Model,
    _spec_dict,
    _transfer_design,
    fit_wind100,
    generator_identity,
    recalibrate,
)

from weathergen import simulate as _sim  # noqa: E402

DB = ROOT.parent / "data" / "pricemodeling.db"
ERA5 = ROOT.parent / "res_model" / "era5_cache"
CUBE = ROOT / "output" / "simulation.nc"
LEGACY = ROOT / "models" / "backup_pre_diurnal" / "wind100.json"
PERIOD = ("2015-01-01", "2026-06-30")
SEAS = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
        6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}
#: ERA5 analysis-window seams: the hour ARRIVING at each carries a jump, at every station, in every year
SEAM_HOURS = (10, 22)
#: generator realizations for the calibration (seed offsets; the default cube is the third), and the held-out one
CAL_SEED_OFFSETS = (101, 202)
HOLDOUT_SEED_OFFSET = 303


def _stations() -> pd.DataFrame:
    """Cube stations with their local-solar-time offset (the diurnal terms are in LST, as the climatology is)."""
    ds = xr.open_dataset(CUBE)
    obs = ds["obs"]
    ids = [str(s) for s in obs["station"].values]
    off = obs["lst_offset_h"].values.astype(float) if "lst_offset_h" in obs.coords else np.zeros(len(ids))
    ds.close()
    return pd.DataFrame({"lst_offset_h": off}, index=pd.Index(ids, name="station"))


def _panel_master(stations: list[str], suffix: str, clip0: bool) -> pd.DataFrame:
    """Wide station panel for `meteo_<station>_<suffix>` from master_hourly (index = ts_utc)."""
    con = sqlite3.connect(DB)
    cols = {r[1] for r in con.execute('PRAGMA table_info("master_hourly")').fetchall()}
    sel = [f'meteo_{s}_{suffix}' for s in stations if f'meteo_{s}_{suffix}' in cols]
    df = pd.read_sql(f'SELECT ts_utc, {", ".join(sel)} FROM master_hourly '
                     f"WHERE ts_utc >= ? AND ts_utc <= ?", con, params=PERIOD)
    con.close()
    df.index = pd.to_datetime(df["ts_utc"], utc=True)
    out = df.drop(columns=["ts_utc"])
    if clip0:
        out = out.clip(lower=0)
    out.columns = [c[len("meteo_"):-len(f"_{suffix}")] for c in out.columns]   # meteo_<station>_<suffix> → station
    return out


def _panel_10m(stations: list[str]) -> pd.DataFrame:
    return _panel_master(stations, "wind_speed_ms", clip0=True)


def _panel_temp(stations: list[str]) -> pd.DataFrame:
    return _panel_master(stations, "temperature_c", clip0=False)


def _panel_100m(stations: list[str]) -> pd.DataFrame:
    cols = {}
    for s in stations:
        f = ERA5 / f"era5_{s}.zip"
        if not f.exists():
            continue
        dest = f.parent / (f.stem + "_x"); dest.mkdir(exist_ok=True)
        with zipfile.ZipFile(f) as z:
            z.extractall(dest)
        ds = xr.open_dataset(str(sorted(dest.glob("*.nc"))[0]))
        if "valid_time" in ds.coords and "time" not in ds.coords:
            ds = ds.rename({"valid_time": "time"})
        t = pd.to_datetime(ds["time"].values, utc=True)
        w100 = np.sqrt(np.asarray(ds["u100"].values, float) ** 2 + np.asarray(ds["v100"].values, float) ** 2)
        cols[s] = pd.Series(w100.ravel(), index=t)
        ds.close()
    return pd.DataFrame(cols)


def _denotch(panel: pd.DataFrame) -> pd.DataFrame:
    """Replace ERA5's analysis-window seam hours by the log-space mean of the hours either side. They arrive with
    a jump in every year at every station and are absent from observed production, so a fit must not learn them."""
    lg = np.log(panel + _EPS)
    prev = lg.shift(1, freq="h").reindex(lg.index)
    nxt = lg.shift(-1, freq="h").reindex(lg.index)
    fill = np.exp((prev + nxt) / 2.0) - _EPS
    out = panel.copy()
    mask = np.isin(panel.index.hour, SEAM_HOURS)
    out.loc[mask] = fill.loc[mask].where(fill.loc[mask].notna(), panel.loc[mask])
    return out


# ------------------------------------------------------------------------------------------ numpy moment helpers
def _mh_index(time) -> np.ndarray:
    t = pd.DatetimeIndex(time)
    return ((t.month.to_numpy() - 1) * 24 + t.hour.to_numpy()).astype(int)


def _anom(x: np.ndarray, g: np.ndarray) -> np.ndarray:
    ok = np.isfinite(x)
    sums = np.bincount(g[ok], weights=x[ok], minlength=288)
    cnt = np.bincount(g[ok], minlength=288)
    return x - (sums / np.maximum(cnt, 1))[g]


def _nat_anom_std(w: np.ndarray, g: np.ndarray, sel: np.ndarray | None = None) -> float:
    a = _anom(np.nanmean(w, axis=1), g)
    return float(np.nanstd(a if sel is None else a[sel]))


def _station_logvar(lg: np.ndarray, g: np.ndarray) -> float:
    return float(np.mean([np.nanvar(_anom(lg[:, j], g)) for j in range(lg.shape[1])]))


def _first_harmonic(p: pd.Series) -> tuple[float, float]:
    """(phase hour, amplitude) of the 24 h fundamental."""
    k = np.arange(24)
    q = p.to_numpy() - p.mean()
    s, c = float((q * np.sin(2 * np.pi * k / 24)).sum()), float((q * np.cos(2 * np.pi * k / 24)).sum())
    return float((np.degrees(np.arctan2(s, c)) / 15.0) % 24), 2.0 * float(np.hypot(s, c)) / 24.0


def _deseason(s: pd.Series) -> pd.Series:
    doy = (s.index.dayofyear.to_numpy() - 1) / 365.25
    X = np.column_stack([np.ones(len(s))] + [f(2 * np.pi * k * doy) for k in (1, 2) for f in (np.sin, np.cos)])
    ok = s.notna().to_numpy()
    c, *_ = np.linalg.lstsq(X[ok], s.to_numpy()[ok], rcond=None)
    return s - X @ c


def _nancorr(a, b) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    return float(np.corrcoef(a[m], b[m])[0, 1])


# --------------------------------------------------------------------------------------------- generator helpers
def _simulate_realization(gen, cfg, seed_offset: int, keep: list[str]) -> xr.DataArray:
    """One generator realization, 10 m wind and temperature only (the co-generation step is skipped)."""
    orig = _sim._append_wind100
    _sim._append_wind100 = lambda cube, config, rng: cube
    try:
        sim = _sim.simulate(gen, cfg, np.random.default_rng(int(cfg.seed) + int(seed_offset)))
    finally:
        _sim._append_wind100 = orig
    return sim.sel(station=keep, variable=["wind_speed_ms", "temperature_c"]).load()


def _mean_part(model: Wind100Model, cube: xr.DataArray) -> np.ndarray:
    """Deterministic transfer mean (log space), per station, without the recalibration."""
    w = cube.sel(variable="wind_speed_ms").transpose("time", "station")
    t = pd.DatetimeIndex(w["time"].values)
    spec = _spec_dict(model.spec)
    offs = w["lst_offset_h"].values.astype(float) if "lst_offset_h" in w.coords else np.zeros(w.shape[1])
    coef = np.asarray(model.coef, float)
    lw = np.log(np.clip(w.values, 0, None) + _EPS)
    out = np.empty(w.shape)
    for j, s in enumerate(map(str, w["station"].values)):
        out[:, j] = _transfer_design(t, float(offs[j]), lw[:, j], spec) @ coef[model.station_ids.index(s)]
    return out


def _unit_residuals(model: Wind100Model, n: int, phi_c: float, rng: np.random.Generator):
    """Unit-variance station AR(1) field (fitted correlation and persistence) and a unit-variance common AR(1)."""
    phi = np.asarray(model.phi, float)
    innov = (rng.standard_normal((n, len(phi))) @ np.asarray(model.chol, float).T) * np.sqrt(1.0 - phi ** 2)[None, :]
    e = np.empty_like(innov)
    for j in range(len(phi)):
        e[:, j] = lfilter([1.0], [1.0, -phi[j]], innov[:, j])
    z0 = rng.standard_normal()
    c, _ = lfilter([1.0], [1.0, -phi_c], rng.standard_normal(n) * np.sqrt(1.0 - phi_c ** 2), zi=[phi_c * z0])
    return e, c


def _report(label: str, model: Wind100Model, holdout: xr.DataArray, era: pd.DataFrame, era_raw: pd.DataFrame,
            hist_temp: pd.DataFrame, rng: np.random.Generator) -> None:
    out = model.append(holdout.sel(variable=["wind_speed_ms"]), rng).sel(variable=VAR_NAME).transpose("time", "station")
    t = pd.DatetimeIndex(out["time"].values)
    arr = out.values
    nat = pd.Series(np.nanmean(arr, axis=1), index=t)
    en = era.mean(axis=1)
    en_raw = era_raw.mean(axis=1)
    print(f"[fit_wind100] {label} on the held-out generator realization (20 years) vs ERA5 2015-26:", flush=True)
    for q in ("ALL", "DJF", "MAM", "JJA", "SON"):
        ms = nat if q == "ALL" else nat[np.asarray(nat.index.month.map(SEAS)) == q]
        me = en if q == "ALL" else en[np.asarray(en.index.month.map(SEAS)) == q]
        mr = en_raw if q == "ALL" else en_raw[np.asarray(en_raw.index.month.map(SEAS)) == q]
        ps, pe = ms.groupby(ms.index.hour).mean(), me.groupby(me.index.hour).mean()
        (phs, ams), (phe, ame) = _first_harmonic(ps), _first_harmonic(pe)
        dph = ((phs - phe + 12) % 24) - 12
        peak_era5 = int(mr.groupby(mr.index.hour).mean().idxmax())
        print(f"    {q}: phase {phs:5.1f} h vs {phe:5.1f} h ({dph:+.1f} h)  amplitude x{ams / ame:.2f}"
              f"  profile corr {np.corrcoef(ps, pe)[0, 1]:.3f}  peak {int(ps.idxmax())} UTC (ERA5 {peak_era5})",
              flush=True)
    lvl = nat.groupby(nat.index.month).mean() / en.groupby(en.index.month).mean()
    print(f"    monthly level x{lvl.min():.3f} .. x{lvl.max():.3f}  (by month: {' '.join(f'{v:.2f}' for v in lvl)})")
    djf_s, djf_e = np.asarray(t.month.isin([12, 1, 2])), np.asarray(en.index.month.isin([12, 1, 2]))
    r = {"mean": nat.mean() / en.mean(), "std": nat.std() / en.std(),
         "p05": nat.quantile(.05) / en.quantile(.05), "p50": nat.quantile(.5) / en.quantile(.5),
         "p95": nat.quantile(.95) / en.quantile(.95), "p99": nat.quantile(.99) / en.quantile(.99),
         "DJF std": nat[djf_s].std() / en[djf_e].std(), "DJF p95": nat[djf_s].quantile(.95) / en[djf_e].quantile(.95)}
    print("    national distribution: " + "  ".join(f"{k} x{v:.3f}" for k, v in r.items()))
    gs, ge = _mh_index(t), _mh_index(era.index)
    era_arr = era.to_numpy()
    print(f"    anomaly std x{_nat_anom_std(arr, gs) / _nat_anom_std(era_arr, ge):.3f} (DJF "
          f"x{_nat_anom_std(arr, gs, djf_s) / _nat_anom_std(era_arr, ge, djf_e):.3f})  station log-anomaly variance "
          f"x{_station_logvar(np.log(arr + _EPS), gs) / _station_logvar(np.log(era_arr + _EPS), ge):.3f}", flush=True)
    la_s = np.column_stack([_anom(np.log(arr[:, j] + _EPS), gs) for j in range(arr.shape[1])])
    la_e = np.column_stack([_anom(np.log(era_arr[:, j] + _EPS), ge) for j in range(era_arr.shape[1])])
    iu = np.triu_indices(arr.shape[1], 1)
    coh_s = float(np.nanmean(pd.DataFrame(la_s).corr().to_numpy()[iu]))
    coh_e = float(np.nanmean(pd.DataFrame(la_e).corr().to_numpy()[iu]))
    step_ratio = nat.diff().std() / en.diff().std()
    print(f"    cross-station coherence {coh_s:.3f} vs {coh_e:.3f}  step std x{step_ratio:.2f}  "
          f"acf lag 1/6/24: {nat.autocorr(1) - en.autocorr(1):+.3f} / {nat.autocorr(6) - en.autocorr(6):+.3f} / "
          f"{nat.autocorr(24) - en.autocorr(24):+.3f}", flush=True)
    tn = pd.Series(holdout.sel(variable="temperature_c").transpose("time", "station").values.mean(axis=1), index=t)
    h_t = hist_temp.mean(axis=1)
    djf_h = np.asarray(h_t.index.month.isin([12, 1, 2]))
    c_sim = _nancorr(_deseason(tn).to_numpy()[djf_s], _deseason(nat).to_numpy()[djf_s])
    c_hist = _nancorr(_deseason(h_t).to_numpy()[djf_h], _deseason(en.reindex(h_t.index)).to_numpy()[djf_h])
    print(f"    within-winter temperature coupling {c_sim:.3f} vs historical {c_hist:.3f}", flush=True)


def main() -> None:
    t0 = time.monotonic()
    meta = _stations()
    stations = list(meta.index)
    print(f"[fit_wind100] {len(stations)} stations", flush=True)
    # temp_panel is DELIBERATELY not passed → transfer-only model. The temperature-conditioning capability
    # exists in fit_wind100 (#79) but is OFF: measured on the historical panels, a local station-level temp term
    # makes the within-winter coupling WORSE (see weathergen/WIND_TEMP_COUPLING.md). Set `USE_TEMP=True` only to
    # reproduce that (rejected) experiment.
    USE_TEMP = False
    w10, w100_raw = _panel_10m(stations), _panel_100m(stations)
    common = w10.index.intersection(w100_raw.index)
    w10, w100_raw = w10.loc[common], w100_raw.loc[common]
    keep = [s for s in stations if s in w10.columns and s in w100_raw.columns]
    w100 = _denotch(w100_raw[keep])
    hist_temp = _panel_temp(keep).reindex(common)
    temp = hist_temp if USE_TEMP else None
    offsets = meta.loc[keep, "lst_offset_h"].to_dict()
    print(f"[fit_wind100] {len(keep)} stations aligned, {len(common)} hours (temp conditioning: "
          f"{'ON' if USE_TEMP else 'OFF'}; diurnal spec {DEFAULT_SPEC}; ERA5 seams at {SEAM_HOURS} UTC interpolated)",
          flush=True)

    # 1. transfer
    model = fit_wind100(w10[keep], w100[keep], temp_panel=temp, lst_offset_h=offsets, spec=DEFAULT_SPEC)
    print(f"[fit_wind100] transfer: R² {model.fit_r2.mean():.3f} (min {model.fit_r2.min():.3f}) | "
          f"b {model.b.mean():.3f} | φ {model.phi.mean():.3f} | σ {model.sigma.mean():.3f}", flush=True)

    # 2. persistence of the residual's national common mode, in sample
    coef = np.asarray(model.coef, float)
    res = pd.DataFrame({s: np.log(w100[s].to_numpy() + _EPS)
                        - _transfer_design(common, offsets[s], np.log(w10[s].to_numpy() + _EPS), DEFAULT_SPEC) @ coef[i]
                        for i, s in enumerate(model.station_ids)}, index=common)
    lags = np.array([6, 12, 24, 48, 96])
    acf = np.array([res.mean(axis=1).autocorr(int(L)) for L in lags])
    ok = acf > 0.02
    slope, _ = np.polyfit(lags[ok], np.log(acf[ok]), 1)
    phi_c = float(np.clip(np.exp(slope), 0.9, 0.999))
    print(f"[fit_wind100] national-mean residual decays as {np.exp(slope):.4f}^lag (e-folding {-1 / slope:.0f} h): "
          f"phi_c = {phi_c:.4f}", flush=True)

    # 3. residual scale and common share, generator-driven
    cfg = load_config(str(ROOT / "config.yaml"))
    gen = FittedModel.load(cfg.models_dir / "fitted.json")
    gen.trend = _build_trend(cfg, argparse.Namespace(ssp=None, target_year=None, trend=None))
    ids = list(model.station_ids)
    cal = [xr.open_dataset(CUBE)["obs"].sel(station=ids, variable=["wind_speed_ms", "temperature_c"]).load()]
    for off in CAL_SEED_OFFSETS:
        tt = time.monotonic()
        cal.append(_simulate_realization(gen, cfg, off, ids))
        print(f"[fit_wind100] generator realization seed+{off}: {time.monotonic() - tt:.0f}s", flush=True)
    ge = _mh_index(common)
    era_arr = w100[ids].to_numpy()
    target_nat = _nat_anom_std(era_arr, ge)
    target_st = _station_logvar(np.log(era_arr + _EPS), ge)
    sig = np.asarray(model.sigma, float)
    parts = []
    for i, cb in enumerate(cal):
        M = _mean_part(model, cb)
        e, c = _unit_residuals(model, M.shape[0], phi_c, np.random.default_rng(int(cfg.seed) + 11 + i))
        parts.append((M, e, c, _mh_index(cb["time"].values)))
    vm = np.mean([_station_logvar(M, g) for M, _, _, g in parts])
    vr = np.mean([np.mean([sig[j] ** 2 * np.nanvar(_anom(e[:, j], g)) for j in range(len(sig))])
                  for _, e, _, g in parts])
    k = float(np.clip(np.sqrt(max(target_st - vm, 0.25 * vr) / vr), 0.5, 1.5))
    lams = np.round(np.arange(0.0, 0.61, 0.025), 3)
    ratio = []
    for lam in lams:
        v = []
        for M, e, c, g in parts:
            r = k * sig[None, :] * (np.sqrt(1.0 - lam) * e + np.sqrt(lam) * c[:, None])
            v.append(_nat_anom_std(np.clip(np.exp(M + r) - _EPS, 0.0, None), g))
        ratio.append(float(np.mean(v)) / target_nat)
    ratio = np.array(ratio)
    if ratio[0] <= 1.0 <= ratio[-1]:
        lam = float(np.interp(1.0, ratio, lams))
    else:
        lam = float(lams[np.argmin(np.abs(ratio - 1.0))])
    print(f"[fit_wind100] residual calibration on {len(cal)} generator realizations: sigma scale k = {k:.3f} "
          f"(station log-anomaly variance target {target_st:.4f}, transfer part {vm:.4f}); "
          f"common share lambda = {lam:.3f} "
          f"(national anomaly std ratio {ratio[0]:.3f} at 0 -> 1.000)", flush=True)
    model = model.with_residual(lam if lam > 0 else None, phi_c, k)

    # 4. recalibration of the national diurnal x seasonal log mean, pooled over the same realizations
    gid = generator_identity(cfg.models_dir)
    rc = recalibrate(model, cal, w100[ids].mean(axis=1), np.random.default_rng(int(cfg.seed) + 1), generator_id=gid)
    print(f"[fit_wind100] recalibration: {len(rc)} terms, pooled over {len(cal)} realizations, generator {gid}",
          flush=True)
    del parts, cal, res

    # 5. held-out realization: pre-fix model and refit model on the same weather
    holdout = _simulate_realization(gen, cfg, HOLDOUT_SEED_OFFSET, ids)
    era_raw = w100_raw[ids]
    if LEGACY.exists():
        _report("pre-fix model", Wind100Model.load(LEGACY), holdout, w100[ids], era_raw, hist_temp[ids],
                np.random.default_rng(int(cfg.seed) + 2))
    _report("refit model", model, holdout, w100[ids], era_raw, hist_temp[ids], np.random.default_rng(int(cfg.seed) + 2))

    out = model.save(ROOT / "models" / "wind100.json")
    print(f"[fit_wind100] saved -> {out}  ({(time.monotonic() - t0) / 60:.1f} min)", flush=True)


if __name__ == "__main__":
    main()
