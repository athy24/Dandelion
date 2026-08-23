"""Co-generation of ERA5 100 m wind conditioned on the simulated 10 m station wind.

Step (iv) flagged that a *deterministic* station-10 m→100 m transfer is a materially weaker hourly
predictor than ERA5-100 m (it compresses variance and loses spatial coherence). The clean fix — per
the step-(iv) spec §2.A — is to co-generate 100 m wind here in step (ii), so the renewables model reads
a physically-realistic 100 m field that stays coherent with the rest of the weather draw.

Per station s:  log(w100) = a_s + b_s·log(w10) + c_s·temp_anom + r_s(t)
with r_s a **spatially-correlated AR(1)** residual — AR(1) gives realistic ramps, the spatial Cholesky
keeps a low-wind lull coherent across France (the Dunkelflaute driver). Fitted on historical
(SYNOP 10 m, ERA5 100 m) pairs; applied to the simulated 10 m wind with the same seeded rng.

The **temperature term** (#79) is the fix for the winter demand–RES correlation gap: 100 m wind couples to
temperature *more* strongly than 10 m (it is more geostrophic/synoptic), but a transfer that routes all
temp coupling through 10 m — plus a temp-independent residual that is 65 % of the variance — makes the
co-generated 100 m wind *less* temp-coupled than 10 m, inverting reality (see weathergen/WIND_TEMP_COUPLING.md).
Adding the **deseasonalized** temperature anomaly as a predictor (c_s > 0: a mild *within-winter* anomaly ⇒
windier — Atlantic low-pressure storms are both mild and windy) restores the direct synoptic coupling the
transfer alone cannot carry. Deseasonalizing matters: the raw temp↔wind sign flips across seasons (calm hot
summers), so only the within-season anomaly carries the winter coupling. Models fitted before this (c=None)
fall back to the transfer-only mean.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from scipy.signal import lfilter

from powersim_core.serialize import load_params, save_params

_EPS = 0.1
VAR_NAME = "wind_speed_100m_ms"


def _doy_design(index: pd.DatetimeIndex) -> np.ndarray:
    """Day-of-year harmonic design [1, cos1, sin1, cos2, sin2] for a seasonal temperature climatology.
    Deseasonalizing is essential (#79): the temp↔wind coupling flips sign between seasons (hot summer =
    calm high pressure vs mild winter = windy Atlantic storms), so only the *within-season* anomaly carries
    the winter coupling the demand–RES correlation needs — raw temperature fits the wrong cross-seasonal sign."""
    doy = pd.DatetimeIndex(index).dayofyear.to_numpy(float)
    w = 2 * np.pi * doy / 365.25
    return np.column_stack([np.ones(len(doy)), np.cos(w), np.sin(w), np.cos(2 * w), np.sin(2 * w)])


def _temp_anomaly(temp: np.ndarray, index: pd.DatetimeIndex, seas: np.ndarray) -> np.ndarray:
    """Deseasonalized temperature anomaly = temp − climatology(day-of-year), given harmonic coeffs `seas`."""
    return temp - _doy_design(index) @ seas



def _haversine_km(lat, lon):
    """Pairwise great-circle distance (km) for station coordinate vectors."""
    la = np.radians(np.asarray(lat, float)); lo = np.radians(np.asarray(lon, float))
    dla = la[:, None] - la[None, :]
    dlo = lo[:, None] - lo[None, :]
    h = np.sin(dla / 2) ** 2 + np.cos(la)[:, None] * np.cos(la)[None, :] * np.sin(dlo / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(np.clip(h, 0, 1)))


def _extend_residual_corr(fitted_chol, known_idx, fitted_pos, lat, lon):
    """Residual correlation over ALL stations: fitted block kept exactly, the rest exp(-d/L).

    The 100 m residual is a SPATIAL field. Giving unfitted stations an INDEPENDENT residual — the obvious
    first fallback — destroys precisely the cross-border correlation the neighbour extension exists to
    create: measured, it collapsed simulated FR-BE 100 m wind correlation from the 10 m field's 0.57 to
    0.21, and BE-NL from 0.84 to 0.29, because the residual variance swamps the correlated transfer.

    So the unknown block is filled by an exponential decay whose length scale L is FITTED from the known
    block's own correlation-vs-distance, and the known-known sub-matrix is preserved unchanged. A nugget
    keeps the result positive-definite for the Cholesky."""
    S = len(lat)
    d = _haversine_km(lat, lon)
    C = np.exp(-d / 500.0)                                  # provisional; L re-fitted just below
    if len(known_idx) > 2:
        R = fitted_chol @ fitted_chol.T
        kk = np.ix_(known_idx, known_idx)
        dk = d[kk][np.triu_indices(len(known_idx), 1)]
        rk = np.clip(R[np.ix_(fitted_pos, fitted_pos)][np.triu_indices(len(known_idx), 1)], 1e-6, 1.0)
        ok = (dk > 1.0) & (rk > 1e-3)
        if ok.sum() > 10:                                   # log r = -d / L  ->  least squares on the slope
            L = float(-dk[ok].sum() / np.log(rk[ok]).sum()) if np.log(rk[ok]).sum() < 0 else 500.0
            L = float(np.clip(L, 100.0, 3000.0))
            C = np.exp(-d / L)
        C[kk] = R[np.ix_(fitted_pos, fitted_pos)]           # keep the fitted structure exactly
    np.fill_diagonal(C, 1.0)
    C = 0.5 * (C + C.T)
    w, V = np.linalg.eigh(C)                                # project to the nearest PSD matrix
    C = (V * np.clip(w, 1e-8, None)) @ V.T
    dg = np.sqrt(np.clip(np.diag(C), 1e-12, None))
    C = C / dg[:, None] / dg[None, :]
    return np.linalg.cholesky(C + 1e-9 * np.eye(S))

@dataclass
class Wind100Model:
    station_ids: list[str]
    a: np.ndarray                   # intercept per station
    b: np.ndarray                   # log-slope (≈ shear) per station
    phi: np.ndarray                 # AR(1) coefficient per station
    sigma: np.ndarray               # stationary residual std (log space) per station
    chol: np.ndarray                # Cholesky of the cross-station residual correlation
    fit_r2: np.ndarray              # per-station transfer R²
    c: np.ndarray | None = None            # temp-anomaly coefficient per station (#79); None → no temp term
    temp_seas: np.ndarray | None = None    # per-station day-of-year climatology harmonic coeffs (S×5)
    temp_astd: np.ndarray | None = None    # per-station std of the DESEASONALIZED temp anomaly

    def append(self, cube: xr.DataArray, rng: np.random.Generator) -> xr.DataArray:
        """Append the co-generated 100 m wind variable to a simulated cube."""
        if VAR_NAME in set(map(str, cube["variable"].values)):
            return cube
        w10 = cube.sel(variable="wind_speed_ms").transpose("time", "station")
        order = [str(s) for s in w10["station"].values]
        known = [s in self.station_ids for s in order]
        if not any(known):                                              # nothing fitted at all (smoke test)
            return cube
        # PER-STATION DEGRADATION, not all-or-nothing. The neighbour extension adds stations this model was
        # never fitted on, and the previous behaviour — skip the whole variable if ANY station is unknown —
        # silently dropped `wind_speed_100m_ms` from the cube entirely, which step iv rejects outright.
        #
        # An unknown station takes the MEDIAN fitted transfer (a, b) and an INDEPENDENT residual. That is an
        # approximation with a known direction: shear is terrain-dependent, so a French-median exponent
        # applied to a North Sea coastal site slightly OVERSTATES the 100 m / 10 m ratio (smoother surface,
        # lower shear) and understates it over rough inland terrain. It is bounded, though: the 10 m field
        # itself is simulated per station by the copula, so the spatial and temporal structure of the wind
        # is real; only the height extrapolation is generic. Downstream users that FIT a response on this
        # same variable absorb the level bias into their own coefficient.
        _pos = [self.station_ids.index(s) if k else -1 for s, k in zip(order, known)]
        def _take(arr, med):
            arr = np.asarray(arr)
            return np.array([arr[i] if i >= 0 else med for i in _pos])
        a = _take(self.a, float(np.median(self.a)))
        b = _take(self.b, float(np.median(self.b)))
        phi = _take(self.phi, float(np.median(self.phi)))
        sigma = _take(self.sigma, float(np.median(self.sigma)))
        kn = [i for i, k in enumerate(known) if k]
        kp = [self.station_ids.index(order[i]) for i in kn]
        if all(known):
            L = self.chol[np.ix_(kp, kp)]                               # every station fitted: exact
        elif "latitude" in cube.coords and "longitude" in cube.coords:
            L = _extend_residual_corr(self.chol, kn, kp,
                                      cube["latitude"].sel(station=w10["station"]).values,
                                      cube["longitude"].sel(station=w10["station"]).values)
        else:
            # no geometry to extend with (synthetic cubes): fitted block, unknowns independent
            L = np.eye(len(order))
            L[np.ix_(kn, kn)] = self.chol[np.ix_(kp, kp)]

        n, S = w10.shape
        mean = a[None, :] + b[None, :] * np.log(np.clip(w10.values, 0, None) + _EPS)
        # #79: direct synoptic temp coupling — add c·(deseasonalized temp anomaly) where the model was fitted
        # with it and the cube carries temperature. The anomaly is temp minus the fit-time day-of-year
        # climatology, so it captures the *within-winter* mild-and-windy coupling (not the cross-seasonal sign).
        if self.c is not None and "temperature_c" in set(map(str, cube["variable"].values)):
            temp = cube.sel(variable="temperature_c").transpose("time", "station").sel(station=w10["station"]).values
            _seas = np.asarray(self.temp_seas)
            _med_seas = np.median(_seas, axis=0)
            seas = np.array([_seas[i] if i >= 0 else _med_seas for i in _pos])   # (S, K)
            clim = _doy_design(pd.DatetimeIndex(w10["time"].values)) @ seas.T    # (n, S)
            _astd = _take(self.temp_astd, float(np.median(self.temp_astd)))
            astd = np.where(_astd == 0, 1.0, _astd)
            temp_anom = (temp - clim) / astd[None, :]
            mean = mean + _take(self.c, float(np.median(self.c)))[None, :] * temp_anom
        # spatially-correlated AR(1) residual field (stationary std = sigma)
        innov = (rng.standard_normal((n, S)) @ L.T) * (sigma * np.sqrt(1.0 - phi ** 2))[None, :]
        resid = np.empty_like(innov)
        for j in range(S):
            resid[:, j] = lfilter([1.0], [1.0, -phi[j]], innov[:, j])
        w100 = np.clip(np.exp(mean + resid) - _EPS, 0.0, None)

        da = xr.DataArray(w100[:, :, None], dims=("time", "station", "variable"),
                          coords={"time": cube["time"], "station": w10["station"],
                                  "variable": [VAR_NAME]})
        for c in ("latitude", "longitude", "elevation", "lst_offset_h"):
            if c in cube.coords:
                da = da.assign_coords({c: ("station", cube[c].sel(station=w10["station"]).values)})
        return xr.concat([cube, da.transpose("time", "station", "variable")], dim="variable")

    def save(self, path: str | Path) -> Path:
        """Portable JSON + npz sidecar for the per-station arrays (no pickle — REVIEW F6)."""
        return save_params(asdict(self), Path(path).with_suffix(".json"))

    @staticmethod
    def load(path: str | Path) -> Wind100Model:
        return Wind100Model(**load_params(Path(path).with_suffix(".json")))


def fit_wind100(w10_panel: pd.DataFrame, w100_panel: pd.DataFrame,
                temp_panel: pd.DataFrame | None = None) -> Wind100Model:
    """Fit the conditional model from aligned (time × station) 10 m and 100 m wind panels.

    If `temp_panel` (station temperature °C) is given, the mean gains a standardized-temperature term
    (#79): ``log(w100) = a + b·log(w10) + c·(temp − temp_mean)/temp_std + r`` — restoring the synoptic
    temp↔wind coupling the transfer alone loses. Without it the fit is the transfer-only model as before."""
    stations = [str(c) for c in w10_panel.columns if c in w100_panel.columns]
    w10_panel, w100_panel = w10_panel[stations], w100_panel[stations]
    idx = pd.DatetimeIndex(w10_panel.index)
    design = _doy_design(idx)                                # day-of-year harmonics for deseasonalization
    a, b, c, phi, sigma, r2, seas, astd = ([] for _ in range(8))
    resid_cols = {}
    for s in stations:
        x = np.log(w10_panel[s].to_numpy() + _EPS)
        y = np.log(w100_panel[s].to_numpy() + _EPS)
        has_temp = temp_panel is not None and s in temp_panel.columns
        if has_temp:
            t = pd.to_numeric(temp_panel[s], errors="coerce").to_numpy(float)
            tok = np.isfinite(t)
            scoef, *_ = np.linalg.lstsq(design[tok], t[tok], rcond=None)   # seasonal climatology
            anom = t - design @ scoef                          # within-season temperature anomaly
            asd = float(np.nanstd(anom)) or 1.0
            ta = anom / asd
            ok = np.isfinite(x) & np.isfinite(y) & np.isfinite(ta)
            cols = [np.ones(ok.sum()), x[ok], ta[ok]]
        else:
            scoef, asd = np.zeros(design.shape[1]), 1.0
            ok = np.isfinite(x) & np.isfinite(y)
            cols = [np.ones(ok.sum()), x[ok]]
        X = np.column_stack(cols)
        coef, *_ = np.linalg.lstsq(X, y[ok], rcond=None)
        r = np.full(len(x), np.nan)
        r[ok] = y[ok] - X @ coef
        ss = float(np.nansum(r[ok] ** 2)); tot = float(np.nansum((y[ok] - y[ok].mean()) ** 2))
        a.append(coef[0]); b.append(coef[1]); c.append(coef[2] if has_temp else 0.0)
        seas.append(scoef); astd.append(asd)
        r2.append(1 - ss / tot if tot > 0 else 0.0)
        rr = pd.Series(r).dropna()
        phi.append(float(rr.autocorr(1)) if len(rr) > 10 else 0.0)
        sigma.append(float(rr.std()))
        resid_cols[s] = pd.Series(r, index=w10_panel.index)
    phi = np.clip(np.array(phi), -0.99, 0.99)
    # cross-station residual correlation → Cholesky (PD-regularised)
    R = pd.DataFrame(resid_cols).corr().to_numpy()
    R = np.nan_to_num(R, nan=0.0); np.fill_diagonal(R, 1.0)
    R = 0.99 * R + 0.01 * np.eye(len(stations))
    chol = np.linalg.cholesky(R)
    temp_fields = ({"c": np.array(c), "temp_seas": np.array(seas), "temp_astd": np.array(astd)}
                   if temp_panel is not None else {})            # no temp → identical to the transfer-only model
    return Wind100Model(station_ids=stations, a=np.array(a), b=np.array(b), phi=phi,
                        sigma=np.array(sigma), chol=chol, fit_r2=np.array(r2), **temp_fields)
