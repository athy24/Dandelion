"""Co-generation of ERA5 100 m wind conditioned on the simulated 10 m station wind.

Step (iv) flagged that a *deterministic* station-10 m→100 m transfer is a materially weaker hourly
predictor than ERA5-100 m (it compresses variance and loses spatial coherence). The clean fix — per
the step-(iv) spec §2.A — is to co-generate 100 m wind here in step (ii), so the renewables model reads
a physically-realistic 100 m field that stays coherent with the rest of the weather draw.

Per station s:  log(w100) = a_s + b_s·log(w10) + c_s·temp_anom + r_s(t)
with r_s a **spatially-correlated AR(1)** residual — AR(1) gives realistic ramps, the spatial Cholesky
keeps a low-wind lull coherent across France (the Dunkelflaute driver). Fitted on historical
(SYNOP 10 m, ERA5 100 m) pairs; applied to the simulated 10 m wind with the same seeded rng.

**Diurnal shear (2026-09-14).** That transfer has no hour term, so its co-generated 100 m wind could only
inherit the 10 m wind's DAYTIME peak: 10 m wind is strongest mid-afternoon, when convective mixing brings
momentum down, while 100 m wind is strongest in the evening and at night, when the surface layer decouples
and the flow aloft is no longer braked. Measured on the 42 French stations 2015-2026, ERA5 100 m wind peaks
at 20 UTC while SYNOP 10 m peaks at 15; the 100/10 ratio runs 1.75 at night and 1.18 at noon; and the old
model's national 100 m wind peaked at 13-14 UTC with a diurnal profile ANTI-correlated with ERA5's (-0.24).
The systematic cycle was sitting in the fitted residual (+0.14 log at 00 UTC, -0.20 at 10 UTC) and was thrown
away, because the residual is simulated as stationary AR(1) noise. Observed French onshore production peaks at
21 UTC, so the old field put wind output into the solar hours and took it out of the evening ramp.

When fitted with a `spec`, the mean becomes
    a + b·log(w10) + Σ seasonal x diurnal harmonics(doy, local solar hour)
                   + log(w10) · Σ diurnal x (1 + seasonal) harmonics
i.e. a stability-dependent intercept AND a stability-dependent shear exponent, both varying by season — the
winter boundary layer does not decouple the way a summer night does. `DEFAULT_SPEC` (seasonal K2 x diurnal K3
intercept, diurnal K2 x seasonal K1 slope, 48 coefficients per station) was chosen out of sample (fit
2015-22, test 2023-26) against simpler and richer forms; a diurnal residual variance was tried and rejected.

**Recalibration.** The transfer is fitted on OBSERVED 10 m wind but driven, at simulate time, by the
GENERATOR's 10 m wind, whose diurnal amplitude is 0.72x SYNOP's by first harmonic (0.77x by max - min, which
the hourly noise inflates). Driving a SYNOP-fitted transfer with that damped input leaves the 100 m peak ~1.4 h
late. `recalibrate` estimates a smooth national correction of the
log mean — seasonal K2 x diurnal K3, about ±7 % — so the co-generated national climatology matches ERA5. It is
a property of the PAIR (this transfer, that 10 m generator) and must be re-estimated whenever either is refit;
`scripts/fit_wind100.py` does both in one run.

The **temperature term** (#79) was built as a fix for the winter demand–RES correlation gap and then REJECTED:
measured, a station-level temperature term made the coupling worse, so the shipped model is fitted without it
(`scripts/fit_wind100.py` USE_TEMP=False) and the capability is dormant. Its original rationale, for the record:
100 m wind couples to
temperature *more* strongly than 10 m (it is more geostrophic/synoptic), but a transfer that routes all
temp coupling through 10 m — plus a temp-independent residual that is 65 % of the variance — makes the
co-generated 100 m wind *less* temp-coupled than 10 m, inverting reality (see weathergen/WIND_TEMP_COUPLING.md).
Adding the **deseasonalized** temperature anomaly as a predictor (c_s > 0: a mild *within-winter* anomaly ⇒
windier — Atlantic low-pressure storms are both mild and windy) restores the direct synoptic coupling the
transfer alone cannot carry. Deseasonalizing matters: the raw temp↔wind sign flips across seasons (calm hot
summers), so only the within-season anomaly carries the winter coupling. Models fitted before this (c=None)
fall back to the transfer-only mean.

**Residual coherence (2026-09-14, after review).** The transfer is fitted on SYNOP 10 m wind, whose station-
specific noise enters both the transfer and its residual, so the fitted residual came out too weakly correlated
across stations (mean 0.11, against 0.28 for ERA5's station log anomalies). Co-generated, each station varied too
much while France as a whole varied too little: national std -8 %, p95 -4.5 %, winter std -12 %, and onshore CF
p95 0.523 against ERA5's 0.567. The residual therefore carries an optional NATIONAL common AR(1) factor:
    r_s = sigma_s * ( sqrt(1 - lambda) * e_s + sqrt(lambda) * c )
with e_s the fitted spatially-correlated station AR(1) and c a unit-variance AR(1) of persistence phi_c. A
station's variance stays sigma_s^2 whatever lambda; cross-station correlation becomes lambda + (1 - lambda) R_ij.
lambda and a sigma scale are calibrated, generator-driven, against ERA5's national anomaly variance and mean
station log-anomaly variance; phi_c comes from the slow decay of the national-mean residual (e-folding ~46 h,
against ~41 h for ERA5's national anomaly). A persistent common factor, rather than simply raising R, is what
stops the national series gaining hour-to-hour roughness it does not have: raising R alone restored the variance
but pushed the national step std from 2.6x to 3.5x ERA5's.

The recalibration belongs to one 10 m generator fit; `recal_generator` records which, and
`simulate._append_wind100` refuses to apply it to another.

Models saved before these changes load with `spec=None`, `recal=None`, `common_share=None` and behave exactly as
they did, consuming the random stream identically.
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

#: the transfer design chosen out of sample (see the module docstring)
DEFAULT_SPEC = {"Ks": 2, "Kd": 3, "Kdb": 2, "Ksb": 1}
#: the national recalibration surface: seasonal K2 x diurnal K3, interacted (35 terms)
RECAL_KS, RECAL_KD = 2, 3


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


def _harmonics(time: pd.DatetimeIndex, offset_h: float, ks: int, kd: int) -> tuple[list, list]:
    """Seasonal and diurnal harmonic columns with the climatology's conventions (`climatology._features`):
    fractional day of year, and the hour in LOCAL SOLAR TIME so the diurnal phase aligns across longitudes."""
    time = pd.DatetimeIndex(time)
    doy = (time.dayofyear.to_numpy() - 1 + time.hour.to_numpy() / 24.0) / 365.25
    lst = (time.hour.to_numpy().astype("float64") + float(offset_h)) % 24.0
    seas = [f(2 * np.pi * k * doy) for k in range(1, ks + 1) for f in (np.sin, np.cos)]
    diur = [f(2 * np.pi * j * lst / 24.0) for j in range(1, kd + 1) for f in (np.sin, np.cos)]
    return seas, diur


def _spec_dict(spec) -> dict:
    s = np.asarray(spec, int).ravel()
    return {"Ks": int(s[0]), "Kd": int(s[1]), "Kdb": int(s[2]), "Ksb": int(s[3])}


def _transfer_design(time, offset_h: float, logw10: np.ndarray, spec: dict,
                     temp_anom: np.ndarray | None = None) -> np.ndarray:
    """[1, log w10, seasonal, diurnal, seasonal x diurnal, log w10 x diurnal, log w10 x seasonal x diurnal, (temp)]."""
    cols = [np.ones(len(logw10)), logw10]
    if spec.get("Kd"):
        seas, diur = _harmonics(time, offset_h, spec["Ks"], spec["Kd"])
        cols += seas + diur + [s * d for s in seas for d in diur]
    if spec.get("Kdb"):
        seasb, diurb = _harmonics(time, offset_h, spec.get("Ksb", 0), spec["Kdb"])
        cols += [logw10 * d for d in diurb] + [logw10 * s * d for s in seasb for d in diurb]
    if temp_anom is not None:
        cols.append(temp_anom)
    return np.column_stack(cols)


def _recal_design(time) -> np.ndarray:
    """The national correction surface, in UTC hours (it was estimated on a national mean)."""
    seas, diur = _harmonics(time, 0.0, RECAL_KS, RECAL_KD)
    return np.column_stack([np.ones(len(pd.DatetimeIndex(time)))] + seas + diur + [s * d for s in seas for d in diur])



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
    a: np.ndarray                   # intercept per station (with `spec`: only the constant term, see `coef`)
    b: np.ndarray                   # log-slope (≈ shear) per station (with `spec`: only the constant slope)
    phi: np.ndarray                 # AR(1) coefficient per station
    sigma: np.ndarray               # stationary residual std (log space) per station
    chol: np.ndarray                # Cholesky of the cross-station residual correlation
    fit_r2: np.ndarray              # per-station transfer R²
    c: np.ndarray | None = None            # temp-anomaly coefficient per station (#79); None → no temp term
    temp_seas: np.ndarray | None = None    # per-station day-of-year climatology harmonic coeffs (S×5)
    temp_astd: np.ndarray | None = None    # per-station std of the DESEASONALIZED temp anomaly
    spec: np.ndarray | None = None         # [Ks, Kd, Kdb, Ksb] diurnal transfer design; None → a + b·log(w10)
    coef: np.ndarray | None = None         # (S × n_design) transfer coefficients when `spec` is set
    lst_offset_h: np.ndarray | None = None # per-station local-solar-time offset used at fit
    recal: np.ndarray | None = None        # national diurnal × seasonal log-mean correction (35,); None → none
    common_share: float | None = None      # lambda: residual variance share carried by a national common AR(1)
    common_phi: float | None = None        # phi_c: persistence of that common factor
    sigma_scale: float | None = None       # k applied to sigma at calibration (audit; `sigma` is stored scaled)
    recal_generator: str | None = None     # identity of the 10 m generator fit `recal` was estimated for

    def with_residual(self, common_share: float | None, common_phi: float | None,
                      sigma_scale: float = 1.0) -> Wind100Model:
        """A copy with the residual's common factor and sigma scale set. Idempotent: the scale applies to the
        UNSCALED sigma, recovered through `sigma_scale`."""
        import copy
        m = copy.deepcopy(self)
        base = np.asarray(self.sigma, float) / float(self.sigma_scale or 1.0)
        m.sigma = base * float(sigma_scale)
        m.sigma_scale = float(sigma_scale)
        m.common_share = float(common_share) if common_share else None
        m.common_phi = float(common_phi) if common_share else None
        return m

    def _diurnal_mean(self, time, logw, pos, cube, w10) -> np.ndarray:
        spec = _spec_dict(self.spec)
        coef = np.asarray(self.coef, float)
        med = np.median(coef, axis=0)
        if "lst_offset_h" in cube.coords:
            offs = np.asarray(cube["lst_offset_h"].sel(station=w10["station"]).values, float)
        else:
            stored = (np.asarray(self.lst_offset_h, float) if self.lst_offset_h is not None
                      else np.zeros(len(self.station_ids)))
            offs = np.array([stored[i] if i >= 0 else 0.0 for i in pos])
        out = np.empty(logw.shape)
        for j in range(logw.shape[1]):
            cj = coef[pos[j]] if pos[j] >= 0 else med                   # unknown station: median transfer
            out[:, j] = _transfer_design(time, float(offs[j]), logw[:, j], spec) @ cj
        return out

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
        # An unknown station takes the MEDIAN fitted transfer — (a, b), or the median `coef` row in its own
        # local solar time under a `spec` model — plus the national recalibration and common factor, and a
        # residual extended from the fitted correlation by distance (independent when the cube has no
        # coordinates). The 10 m input of those stations is ERA5-like rather than SYNOP-like, so neither the
        # transfer nor the recalibration was estimated for it: their 100 m diurnal cycle is untested, and no
        # price consumer reads it. The height extrapolation itself is an approximation with a known direction:
        # shear is terrain-dependent, so a French-median exponent
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
        time = pd.DatetimeIndex(w10["time"].values)
        logw = np.log(np.clip(w10.values, 0, None) + _EPS)
        if self.spec is not None and self.coef is not None:
            # stability-dependent intercept and shear exponent, by local solar hour and season (see the docstring)
            mean = self._diurnal_mean(time, logw, _pos, cube, w10)
        else:
            mean = a[None, :] + b[None, :] * logw
        # #79: direct synoptic temp coupling — add c·(deseasonalized temp anomaly) where the model was fitted
        # with it and the cube carries temperature. The anomaly is temp minus the fit-time day-of-year
        # climatology, so it captures the *within-winter* mild-and-windy coupling (not the cross-seasonal sign).
        if self.c is not None and "temperature_c" in set(map(str, cube["variable"].values)):
            temp = cube.sel(variable="temperature_c").transpose("time", "station").sel(station=w10["station"]).values
            _seas = np.asarray(self.temp_seas)
            _med_seas = np.median(_seas, axis=0)
            seas = np.array([_seas[i] if i >= 0 else _med_seas for i in _pos])   # (S, K)
            clim = _doy_design(time) @ seas.T                                    # (n, S)
            _astd = _take(self.temp_astd, float(np.median(self.temp_astd)))
            astd = np.where(_astd == 0, 1.0, _astd)
            temp_anom = (temp - clim) / astd[None, :]
            mean = mean + _take(self.c, float(np.median(self.c)))[None, :] * temp_anom
        if self.recal is not None:                                      # national generator-pair correction
            mean = mean + (_recal_design(time) @ np.asarray(self.recal, float))[:, None]
        # spatially-correlated AR(1) residual field (stationary std = sigma)
        innov = (rng.standard_normal((n, S)) @ L.T) * (sigma * np.sqrt(1.0 - phi ** 2))[None, :]
        resid = np.empty_like(innov)
        for j in range(S):
            resid[:, j] = lfilter([1.0], [1.0, -phi[j]], innov[:, j])
        if self.common_share:
            # national common AR(1) factor (see the docstring), drawn AFTER the station block so a model without
            # it consumes the random stream exactly as before; started from its stationary distribution
            lam, pc = float(self.common_share), float(self.common_phi)
            z0 = rng.standard_normal()
            x = rng.standard_normal(n) * np.sqrt(1.0 - pc ** 2)
            common, _ = lfilter([1.0], [1.0, -pc], x, zi=[pc * z0])
            resid = np.sqrt(1.0 - lam) * resid + np.sqrt(lam) * sigma[None, :] * common[:, None]
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
                temp_panel: pd.DataFrame | None = None,
                lst_offset_h: dict[str, float] | pd.Series | None = None,
                spec: dict | None = None) -> Wind100Model:
    """Fit the conditional model from aligned (time × station) 10 m and 100 m wind panels.

    If `temp_panel` (station temperature °C) is given, the mean gains a standardized-temperature term
    (#79): ``log(w100) = a + b·log(w10) + c·(temp − temp_mean)/temp_std + r`` — restoring the synoptic
    temp↔wind coupling the transfer alone loses. Without it the fit is the transfer-only model as before.

    If `spec` is given (normally `DEFAULT_SPEC`), the mean also carries the diurnal × seasonal intercept and
    shear-exponent terms, in local solar time from `lst_offset_h` (station → hours; 0 where absent). Without
    `spec` the fit is exactly the pre-2026-09-14 form."""
    stations = [str(c) for c in w10_panel.columns if c in w100_panel.columns]
    w10_panel, w100_panel = w10_panel[stations], w100_panel[stations]
    idx = pd.DatetimeIndex(w10_panel.index)
    design = _doy_design(idx)                                # day-of-year harmonics for deseasonalization
    offsets = {str(k): float(v) for k, v in dict(lst_offset_h).items()} if lst_offset_h is not None else {}
    a, b, c, phi, sigma, r2, seas, astd, coefs = ([] for _ in range(9))
    resid_cols = {}
    for s in stations:
        x = np.log(w10_panel[s].to_numpy() + _EPS)
        y = np.log(w100_panel[s].to_numpy() + _EPS)
        has_temp = temp_panel is not None and s in temp_panel.columns
        ta = None
        if has_temp:
            t = pd.to_numeric(temp_panel[s], errors="coerce").to_numpy(float)
            tok = np.isfinite(t)
            scoef, *_ = np.linalg.lstsq(design[tok], t[tok], rcond=None)   # seasonal climatology
            anom = t - design @ scoef                          # within-season temperature anomaly
            asd = float(np.nanstd(anom)) or 1.0
            ta = anom / asd
            ok = np.isfinite(x) & np.isfinite(y) & np.isfinite(ta)
        else:
            scoef, asd = np.zeros(design.shape[1]), 1.0
            ok = np.isfinite(x) & np.isfinite(y)
        if spec is None:
            cols = [np.ones(ok.sum()), x[ok]] + ([ta[ok]] if has_temp else [])
            X = np.column_stack(cols)
        else:
            X = _transfer_design(idx, offsets.get(s, 0.0), x, spec, ta if has_temp else None)[ok]
        coef, *_ = np.linalg.lstsq(X, y[ok], rcond=None)
        r = np.full(len(x), np.nan)
        r[ok] = y[ok] - X @ coef
        ss = float(np.nansum(r[ok] ** 2)); tot = float(np.nansum((y[ok] - y[ok].mean()) ** 2))
        a.append(coef[0]); b.append(coef[1]); c.append(coef[-1] if has_temp else 0.0)
        if spec is not None:
            coefs.append(coef[:-1] if has_temp else coef)
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
    diurnal_fields = ({"spec": np.array([spec["Ks"], spec["Kd"], spec.get("Kdb", 0), spec.get("Ksb", 0)], dtype=int),
                       "coef": np.array(coefs),
                       "lst_offset_h": np.array([offsets.get(s, 0.0) for s in stations])}
                      if spec is not None else {})
    return Wind100Model(station_ids=stations, a=np.array(a), b=np.array(b), phi=phi,
                        sigma=np.array(sigma), chol=chol, fit_r2=np.array(r2), **temp_fields, **diurnal_fields)


def generator_identity(models_dir: str | Path) -> str:
    """Identity of the fitted 10 m generator (`fitted.json` and its npz sidecar) a recalibration belongs to.
    Files above 64 MB are hashed on size, head and tail: the 2.8 GB sidecar is a zip whose central directory, at
    the tail, carries every member's CRC32 (the argument `model_fingerprint` relies on too)."""
    import hashlib
    d = Path(models_dir)
    h = hashlib.sha256()
    chunk = 16 * 1024 * 1024
    for f in (d / "fitted.json", d / "fitted.json.npz"):
        if not f.exists():
            h.update(f"{f.name}:ABSENT".encode())
            continue
        size = f.stat().st_size
        h.update(f"{f.name}:{size}".encode())
        with f.open("rb") as fh:
            if size <= 64 * 1024 * 1024:
                h.update(fh.read())
            else:
                h.update(fh.read(chunk))
                fh.seek(size - chunk)
                h.update(fh.read(chunk))
    return h.hexdigest()[:16]


def recalibrate(model: Wind100Model, cube, target_national: pd.Series,
                rng: np.random.Generator, stations: list[str] | None = None,
                generator_id: str | None = None) -> np.ndarray:
    """Estimate `model.recal` so the co-generated NATIONAL climatology by (month, hour) matches `target_national`.

    `cube` — one DataArray, or a list of them pooled — must carry `wind_speed_ms` from the GENERATOR (not
    observations) for the stations that make up the target. Pooling several realizations keeps one decade's
    sampling anomaly out of the month-level terms; `target_national` is the hourly national-mean ERA5 100 m wind
    over the same stations. The log ratio
    of the two (month × hour) climatologies is smoothed onto the seasonal K2 × diurnal K3 surface — a smooth
    surface on purpose, so a one-hour artefact in the target (ERA5's 10 UTC analysis-window notch) is not
    copied into every simulated year. Returns the coefficients and sets them on the model."""
    model.recal = None
    cubes = list(cube) if isinstance(cube, (list, tuple)) else [cube]
    sums = counts = None
    for cb in cubes:
        out = model.append(cb.sel(variable=["wind_speed_ms"]), rng).sel(variable=VAR_NAME).transpose("time", "station")
        use = stations or [s for s in map(str, out["station"].values) if s in model.station_ids]
        nat = pd.Series(np.nanmean(out.sel(station=use).values, axis=1), index=pd.DatetimeIndex(out["time"].values))
        g = nat.groupby([nat.index.month, nat.index.hour])
        sums = g.sum() if sums is None else sums.add(g.sum(), fill_value=0.0)
        counts = g.count() if counts is None else counts.add(g.count(), fill_value=0.0)
    tgt = pd.Series(np.asarray(target_national, float), index=pd.DatetimeIndex(target_national.index))
    sim_mh = np.log(sums / counts)
    tgt_mh = np.log(tgt.groupby([tgt.index.month, tgt.index.hour]).mean())
    delta = (tgt_mh - sim_mh).dropna()
    grid = pd.DatetimeIndex([pd.Timestamp(2001, int(m), 15, int(h)) for m, h in delta.index])
    coef, *_ = np.linalg.lstsq(_recal_design(grid), delta.to_numpy(), rcond=None)
    model.recal = coef
    model.recal_generator = generator_id
    return coef
