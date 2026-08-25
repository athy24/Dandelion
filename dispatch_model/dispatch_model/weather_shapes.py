"""Weather-coherent projection net-load shapes (#77) — the provider behind the projection's `weather_shapes`
hook, so a projected year sees a *re-drawn* weathergen weather shape instead of the fixed 2019 one.

FR is exact: its demand (`demand_model`, step iii) and RES (`res_model`, step iv) models both consume the
SAME weathergen realization, so `fr_shape` just runs them and assembles the net load. Neighbours have no such
models, so `NeighbourWeatherModel` is a **reduced-form** stand-in fitted from history: neighbour load responds
to the FR national temperature (HDD/CDD + calendar), and neighbour must-take RES follows the zone's OWN
observed shape (#157) modulated by the FR draw's renewable anomaly vs FR normal — justified by the strong
spatial correlation of European weather (a cold, calm French day is usually cold and calm across the
interconnected core). It is weather-*coherent* (driven by the same FR draw) but not station-resolved; a full
build would extend weathergen to neighbour stations and fit per-zone demand/RES models (steps iii/iv ×6).
RES *levels* are set by the projected neighbour capacity (TYNDP, #76).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]     # repo root (…/PriceModeling)
_MUSTTAKE = ["solar", "wind_onshore", "wind_offshore", "hydro_ror"]

#: per-process caches (never pickled across the MC pool boundary — each worker fills its own). `fr_draw`
#: re-runs the FULL demand+RES horizon on every call, so a trajectory's 20 years MUST share one draw or the
#: projection is ~20× too slow; the neighbour fit is a one-shot OLS over history. Keyed by realization /
#: fit-year so distinct MC draws stay independent.
_DRAW_CACHE: dict[int, pd.DataFrame] = {}
_NB_MODEL_CACHE: dict = {}
#: neighbour structural growth fallback when a zone has no TYNDP row (mirrors projection._GROWTH).
_G_FALLBACK = {"demand": 0.008, "res": 0.045}


# ----------------------------------------------------------------- FR (exact) ---------------------------
def fr_draw(realization: int = 0) -> pd.DataFrame:
    """Run the FR demand + RES models on one weathergen realization → hourly [load_mw, res_mw, temp_nat] over
    the whole projection horizon (one coherent draw). `temp_nat` (projected FR national temperature) drives
    the reduced-form neighbour models; `res_mw` normalised gives the neighbour RES shape.

    Cached per realization (per process): the demand + RES models run ONCE per draw, so all of a trajectory's
    target years slice the same coherent draw instead of recomputing the whole horizon each year."""
    if realization in _DRAW_CACHE:
        return _DRAW_CACHE[realization]
    from demand_model.config import load_config as dm_load
    from demand_model.projection.engine import project_trajectory
    from res_model.config import load_config as rm_load
    from res_model.projection.drivers import projection_drivers
    from res_model.projection.engine import Projector

    rm_cfg = rm_load(str(_ROOT / "res_model" / "config.yaml"))
    load = project_trajectory(dm_load(str(_ROOT / "demand_model" / "config.yaml")),
                              "reference", realization=realization, with_residual=False)
    prod = Projector(rm_cfg).production("reference", realization=realization, with_residual=False)
    temp = projection_drivers(rm_cfg, realization)["temp_nat"]
    df = pd.DataFrame({"load_mw": load, "res_mw": prod["national_total"], "temp_nat": temp}).dropna()
    _DRAW_CACHE[realization] = df
    return df


def fr_shape(target_year: int, realization: int = 0, draw: pd.DataFrame | None = None) -> pd.DataFrame:
    """FR net-load shape [timestamp_utc, demand_mw, musttake_res_mw] for `target_year` from the coherent
    demand+RES draw."""
    d = draw if draw is not None else fr_draw(realization)
    y = d[d.index.year == target_year]
    return pd.DataFrame({"timestamp_utc": y.index, "demand_mw": y["load_mw"].to_numpy(),
                         "musttake_res_mw": y["res_mw"].to_numpy()}).reset_index(drop=True)


# -------------------------------------------------- neighbours (reduced-form) ----------------------------
def _fr_hist_weather(config) -> pd.DataFrame:
    """Historical FR national temperature (°C) hourly from master_hourly — the neighbour demand driver."""
    import sqlite3
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, meteo_temperature_c_fr AS temperature_c FROM master_hourly "
                         "WHERE meteo_temperature_c_fr IS NOT NULL", con)
    finally:
        con.close()
    df["timestamp_utc"] = pd.to_datetime(df["ts_utc"], utc=True)   # DB raw ts_utc → canonical (ADR-3)
    return df.set_index("timestamp_utc")[["temperature_c"]].sort_index()


def _design(temp: np.ndarray, idx: pd.DatetimeIndex) -> np.ndarray:
    """Demand design: [1, HDD, CDD, hour harmonics, weekend] — a temperature-response load model."""
    hdd = np.maximum(16.0 - temp, 0.0)                       # heating degrees below 16 °C
    cdd = np.maximum(temp - 22.0, 0.0)                       # cooling degrees above 22 °C
    h = idx.hour.to_numpy(float)
    wknd = (idx.dayofweek.to_numpy() >= 5).astype(float)
    return np.column_stack([np.ones(len(idx)), hdd, cdd,
                            np.sin(2 * np.pi * h / 24), np.cos(2 * np.pi * h / 24),
                            np.sin(4 * np.pi * h / 24), np.cos(4 * np.pi * h / 24), wknd])


def _norm_shape(series: pd.Series, year: int) -> np.ndarray:
    """A must-take RES hourly series → **unit-mean** numpy array over the full `year` calendar (missing hours
    → 0). The positional array is what a target year slices — the zone's own diurnal/seasonal RES climatology."""
    idx = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h", tz="UTC")[:-1]
    s = series.reindex(idx).fillna(0.0).to_numpy(float)
    m = s.mean()
    return s / m if m > 0 else np.ones_like(s)


_ZONE_HIST_CACHE: dict = {}


def zone_hist_weather(config) -> dict:
    """{zone: DataFrame[temperature_c, wind_speed_100m_ms, cloud_cover_pct]} — OBSERVED per-zone weather.

    Written by `weathergen fit` (`output/zone_hist.parquet`) once the cube carries non-French stations.
    Returns {} on a France-only cube, and every caller must degrade to the FR-temperature proxy rather
    than fail: the France-only cube stays valid, it is just less good for neighbours."""
    if "d" in _ZONE_HIST_CACHE:
        return _ZONE_HIST_CACHE["d"]
    out: dict = {}
    try:
        f = _ROOT / "weathergen" / "output" / "zone_hist.parquet"
        if f.exists():
            df = pd.read_parquet(f)
            df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
            for z, g in df.groupby("zone"):
                out[str(z)] = g.drop(columns=["zone"]).set_index("timestamp_utc").sort_index()
    except Exception:                                    # noqa: BLE001 — never break the fit on this
        out = {}
    _ZONE_HIST_CACHE["d"] = out
    return out


def _res_design(w100: np.ndarray, cloud: np.ndarray, clim: np.ndarray) -> np.ndarray:
    """RES design: [1, wind^3, clearness x climatological shape]. Wind is the 10 m field, which exists in
    BOTH the observed record and the simulated cube (100 m is co-generated at simulate time only); the
    fitted coefficient absorbs the near-constant hub-height shear.

    Wind power is cubic in speed below rated; the climatological shape carries the solar GEOMETRY (diurnal
    and seasonal), so clearness only has to modulate it — which avoids computing solar elevation per zone
    and keeps the fit to three coefficients on data that is often only one year long."""
    wind = np.clip(np.nan_to_num(w100), 0.0, 25.0) ** 3
    clear = np.clip(1.0 - np.nan_to_num(cloud, nan=50.0) / 100.0, 0.0, 1.0)
    return np.column_stack([np.ones(len(wind)), wind / 1e3, clear * clim])


class NeighbourWeatherModel:
    """Reduced-form per-zone demand + **own-weather** RES response to the FR national weather draw.

    Demand is the zone's own load regressed on the FR national temperature (spatially-correlated European
    weather; no neighbour station data exists). RES uses the zone's OWN observed must-take shape (#157) —
    German solar peaks at German solar hours, not French — modulated by the FR draw's renewable *anomaly vs
    FR normal*, which keeps it weather-coherent across zones and draws (a low-wind European draw lowers every
    zone together) while the diurnal/seasonal shape stays each zone's own. Levels come from projected
    capacity (TYNDP growth). A full build would station-resolve neighbours (steps iii/iv ×6)."""

    def __init__(self, load_coef: dict, res_beta: dict, res_shape: dict, fr_res_clim: np.ndarray,
                 res_coef: dict | None = None, own_weather: set | None = None):
        self.res_coef = res_coef or {}    # zone -> OLS coef on _res_design (own wind + own clearness)
        self.ror_shape: dict = {}         # zone -> unit-mean run-of-river climatology (own driver: hydro)
        self.sw_shape: dict = {}          # zone -> unit-mean solar+wind climatology
        self.own_weather = own_weather or set()   # zones fitted on THEIR weather, not the FR proxy
        self.load_coef = load_coef        # zone -> OLS coef on _design (mean load level absorbed by intercept)
        self.res_beta = res_beta          # zone -> {mean_res_mw, mean_load_mw}: RES level anchor
        self.res_shape = res_shape        # zone -> unit-mean OWN must-take RES climatology (fit-year hourly)
        self.fr_res_clim = fr_res_clim    # unit-mean FR must-take RES climatology — the coherence baseline

    @staticmethod
    def fit(config, year: int = 2019) -> NeighbourWeatherModel:
        """Fit each neighbour's load↔FR-temp OLS and store its own must-take RES climatology (fit-year hourly),
        plus the FR RES climatology that the draw is compared against for coherence."""
        from .io.entsoe_hist import load_demand_hist, load_generation_hist
        from .neighbours.blocks import constituents

        temperature = _fr_hist_weather(config)
        load_coef, res_beta, res_shape = {}, {}, {}
        res_coef, own_weather = {}, set()
        ror_shape, sw_shape = {}, {}
        zhist = zone_hist_weather(config)          # {} on a France-only cube -> FR-proxy fallback
        # FR own must-take RES climatology — the baseline the FR draw's anomaly is measured against (so a
        # windier-than-FR-normal draw lifts every neighbour's own shape proportionally). Falls back to a flat
        # unit shape if FR generation history is unavailable (then coherence ≈ the draw's own unit-mean shape).
        try:
            _frg = load_generation_hist(config, year, zones=constituents("FR"))
            _frmt = _frg[_frg["tech"].isin(_MUSTTAKE)].groupby("timestamp_utc")["gen_mw"].sum()
            fr_res_clim = _norm_shape(_frmt, year)
        except (KeyError, ValueError):
            fr_res_clim = np.ones(len(pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h")) - 1)
        # GB used to be excluded alongside FR for want of ENTSO-E history; `pricemodeling.elexon` now
        # supplies its load and generation, so it fits like any other neighbour. FR stays out because it
        # is the reference whose weather drives every other zone's fit, not a zone being fitted.
        zones = [z for z in config.all_zones if z != "FR"]
        for z in zones:
            _parts = constituents(z)
            _raw = load_demand_hist(config, year, zones=_parts)
            ld = _raw.groupby("timestamp_utc")["load_mw"].sum()
            # PER-CONSTITUENT COVERAGE, because a COMPOSITE zone can lose a whole country in silence.
            # `AT_SI` is AT+SI, but SI was absent from the lake before 2024 while AT ran from 2019, so the
            # 2019 fit summed ONE constituent and recorded an Austria-only anchor (63.1 TWh) as if it were
            # both (workbook 87.0). The sufficiency guard below could not catch it: it tests the COMPOSITE,
            # and 8760 valid Austrian hours clear a 500-hour threshold effortlessly. Worse than a level
            # error, it dropped Slovenia's demand while keeping Austria's hydro-heavy must-take, leaving
            # the zone structurally surplus — a direct cause of AT_SI pricing <= 0 in 6596 hours of 2040.
            if len(_parts) > 1 and not _raw.empty and "zone" in _raw.columns:
                _seen = {str(k) for k, v in _raw.groupby("zone")["load_mw"].count().items() if v >= 500}
                if (_missing := [c for c in _parts if c not in _seen]):
                    print(f"[weather_shapes] WARNING {z} fit-year {year}: constituent(s) {_missing} have "
                          f"no usable load history; the zone's demand anchor covers {sorted(_seen)} only "
                          f"and is understated.", flush=True)
            # THE SUFFICIENCY GUARD STAYS AHEAD OF `load_generation_hist`. An earlier version of this
            # correction hoisted the generation read above it, which crashed the whole fit with
            # `KeyError: 'tech'` on the first zone that has load rows but no generation rows — an empty
            # frame has no columns. The fit is the shipped default for weather-coherent projection, so
            # that turned a silent degrade into a hard failure for every Monte Carlo trajectory.
            if len(pd.DataFrame({"load": ld}).join(temperature, how="inner").dropna()) < 500:
                continue
            g = load_generation_hist(config, year, zones=constituents(z))
            mt = (g[g["tech"].isin(_MUSTTAKE)].groupby("timestamp_utc")["gen_mw"].sum()
                  if not g.empty and "tech" in g.columns else pd.Series(dtype=float))
            # RUN-OF-RIVER IS SPLIT OUT, because it does not grow when a country builds solar panels.
            # `_MUSTTAKE` mixes hydro_ror with solar and wind, and the whole basket used to be scaled by a
            # factor derived from `cap_solar_gw + cap_wind_gw` alone. On hydro-heavy zones that is a
            # category error with a large number attached: AT_SI's 2019 must-take is 76 % Austrian
            # run-of-river and its 2040 factor is 7.39, so Austria's rivers were modelled at 7.4x their
            # flow — 30.5 GW of mean "RES" against a 14.3 GW peak demand, which pinned the zone at the
            # curtailment bid in all 8736 hours of the year. IT-North was overstated 3.09x, CH 2.71x.
            ror = (g[g["tech"] == "hydro_ror"].groupby("timestamp_utc")["gen_mw"].sum()
                   if not g.empty and "tech" in g.columns else pd.Series(dtype=float))
            ror = ror.reindex(mt.index).fillna(0.0) if len(mt) else ror
            # `_MUSTTAKE` alone reads NL's solar as a 0.4 GW stub, because TenneT reports the Dutch fleet
            # under `Other` (see `io.unclassified_gen`). This path is the one the Monte Carlo trajectories
            # use, so it needs the same correction `neighbour_netload` applies to the backtest — otherwise
            # a projected NL keeps a net load its real system never sees, and never prices a negative hour.
            # The must-run part lands on LOAD (it is not weather-driven and must not enter `res_shape`,
            # which is scaled by the draw's renewable anomaly); the solar part lands on must-take.
            #
            # It is applied BEFORE the temperature regression, not after: `load_coef` is what `shape()`
            # evaluates to produce a projected `load_mw`, so fitting it on gross load while reporting a net
            # `mean_load_mw` would leave the two halves of this model describing different quantities.
            #
            # ALL THREE CORRECTIONS, IN `neighbour_netload`'s ORDER. The backtest applies gb_embedded ->
            # unclassified -> ch_hydro; this fit applied only the middle one, so the calibrated model and
            # the projected one were not the same model. Order is not cosmetic: gb_embedded floors load at
            # zero after subtracting, and unclassified also moves load, so running them in the other order
            # can clip differently.
            _d = pd.DataFrame({"load_mw": ld, "musttake_res_mw": mt.reindex(ld.index).fillna(0.0)})
            # GB only: Elexon meters generation at the TRANSMISSION boundary while ITSDO demand is already
            # net of distribution-connected plant, so the two feeds do not close — 5.85 GW mean on 2024,
            # 17.0 % of GB demand on this fit year. Netting it off load is what the backtest does; leaving
            # it in made projected British demand gross against a stack that never had to serve it.
            from .io.gb_embedded import apply_to_netload as apply_gb_embedded
            _d = apply_gb_embedded(config, z, year, _d)
            from .io.unclassified_gen import apply_to_netload as apply_unclassified
            _d = apply_unclassified(config, z, year, _d)
            # CH only: ~11.8 TWh/yr of run-of-river ENTSO-E never metered before its 2025 feed repair.
            # The BACKTEST has carried this since `io.ch_hydro` (see `neighbours.blocks`); this fit did
            # NOT, so calibration and projection described two different Switzerlands — the projected one
            # missing five sixths of its river. Same correction, same position in the order, one country.
            from .io.ch_hydro import apply_to_netload as apply_ch_ror
            _d = apply_ch_ror(config, z, year, _d)
            ld, mt = _d["load_mw"], _d["musttake_res_mw"]
            # `apply_unclassified` adds recovered SOLAR, so it lands in the solar+wind half by construction
            ror = ror.reindex(mt.index).fillna(0.0) if len(mt) else ror
            # ...but `apply_ch_ror` adds recovered RIVER, and `sw = mt - ror` would hand every added MW to
            # the solar+wind half, where `shape()` multiplies it by the zone's SOLAR+WIND growth factor
            # (CH 2040: x10.06) rather than the hydro factor. That turns 1.35 GW of Swiss river into
            # 13.6 GW — 107 TWh/yr in a country that consumes 60, and it would arrive silently. The river
            # therefore enters BOTH series: `mt - ror` is then unchanged hour for hour, so `sw` stays the
            # solar+wind it always was, and the addition rides `ror_growth` where it belongs.
            #
            # Three fitted artefacts depend on this, not one: `mean_sw_mw` (the level), `sw_shape` (which
            # would otherwise carry the April-July freshet as "solar+wind climatology"), and `res_coef`,
            # the own-weather OLS that regresses `sw` on wind cubed and clearness — a river in its target
            # makes that response meaningless.
            if "ch_missing_ror_mw" in _d.columns:
                ror = ror + _d["ch_missing_ror_mw"].reindex(ror.index).fillna(0.0)
            sw = (mt - ror).clip(lower=0.0)
            # OWN-ZONE WEATHER where the cube provides it, the FR proxy only as a fallback. Regressing a
            # Danish load on French temperature forces a correlation of 1 between their heating demand,
            # which makes European peaks look simultaneous and imports look available exactly when they
            # would not be — the bias that matters most for adequacy.
            zw = zhist.get(z)
            temp_z = zw[["temperature_c"]] if zw is not None and "temperature_c" in zw else temperature
            j = pd.DataFrame({"load": ld}).join(temp_z, how="inner").dropna()
            if len(j) < 500:
                j = pd.DataFrame({"load": ld}).join(temperature, how="inner").dropna()
                zw = None
                if len(j) < 500:
                    continue
            X = _design(j["temperature_c"].to_numpy(), pd.DatetimeIndex(j.index))
            beta, *_ = np.linalg.lstsq(X, j["load"].to_numpy(), rcond=None)
            load_coef[z] = beta.tolist()
            if zw is not None:
                own_weather.add(z)
            # RES: the zone's OWN observed must-take shape (unit-mean) + its mean level (#157).
            res_beta[z] = {"mean_res_mw": float(mt.mean()) if len(mt) else 0.0,
                           "mean_ror_mw": float(ror.mean()) if len(ror) else 0.0,
                           "mean_sw_mw": float(sw.mean()) if len(sw) else 0.0,
                           "mean_load_mw": float(ld.mean())}
            res_shape[z] = _norm_shape(mt, year)
            ror_shape[z] = _norm_shape(ror, year) if len(ror) and ror.mean() > 0 else None
            sw_shape[z] = _norm_shape(sw, year) if len(sw) and sw.mean() > 0 else None
            # own-weather RES response, where the zone has both its own weather and a must-take series
            if zw is not None and len(mt) and {"wind_speed_ms", "cloud_cover_pct"} <= set(zw.columns):
                jr = pd.DataFrame({"res": sw}).join(zw, how="inner").dropna()
                if len(jr) >= 500:
                    clim = _norm_shape(sw, year)
                    pos = pd.DatetimeIndex(jr.index).dayofyear.to_numpy() * 24 +                         pd.DatetimeIndex(jr.index).hour.to_numpy()
                    cl = np.resize(clim, max(pos.max() + 1, len(clim)))[np.clip(pos, 0, len(clim) - 1)]
                    Xr = _res_design(jr["wind_speed_ms"].to_numpy(),
                                     jr["cloud_cover_pct"].to_numpy(), cl)
                    br, *_ = np.linalg.lstsq(Xr, jr["res"].to_numpy(), rcond=None)
                    res_coef[z] = br.tolist()
        m = NeighbourWeatherModel(load_coef, res_beta, res_shape, fr_res_clim,
                                  res_coef=res_coef, own_weather=own_weather)
        m.ror_shape, m.sw_shape = ror_shape, sw_shape
        return m

    def shape(self, zone: str, target_year: int, fr_temp: pd.Series, fr_res_cf: pd.Series,
              load_growth: float = 1.0, res_growth: float = 1.0,
              zone_weather: pd.DataFrame | None = None,
              ror_growth: float | None = None) -> pd.DataFrame | None:
        """Neighbour net-load [timestamp_utc, load_mw, musttake_res_mw] for `target_year`: load from the
        temperature response × growth; RES from the zone's OWN shape × the FR draw's renewable anomaly
        (draw/FR-climatology) × scaled mean × growth (#157 — own-weather, still draw-coherent)."""
        if zone not in self.load_coef:
            return None
        idx = fr_temp.index[fr_temp.index.year == target_year]
        if len(idx) == 0:
            return None
        n = len(idx)
        # The DRAW's own-zone weather where the cube carries it; the FR series only as the fallback the
        # France-only cube forces. `own_weather` records which zones were FITTED that way — evaluating an
        # FR-fitted coefficient set on German temperature would be worse than the proxy it replaces.
        zw = zone_weather if (zone_weather is not None and zone in self.own_weather) else None
        if zw is not None and "temperature_c" in zw.columns:
            t = zw["temperature_c"].reindex(idx).to_numpy()
            if not np.isfinite(t).any():
                t, zw = fr_temp.reindex(idx).to_numpy(), None
        else:
            t = fr_temp.reindex(idx).to_numpy()
            zw = None
        load = (_design(t, pd.DatetimeIndex(idx)) @ np.array(self.load_coef[zone])) * load_growth
        # own-weather RES: zone's own climatology × draw-vs-FR-normal anomaly (bounded), positional-aligned.
        own = np.resize(self.res_shape[zone], n) if zone in self.res_shape else np.ones(n)
        # Run-of-river on ITS OWN driver (`cap_hydro_gw`), solar+wind on theirs. With no hydro factor
        # supplied the caller gets the previous behaviour, so a caller not yet updated cannot silently
        # lose the river — it keeps scaling with the RES factor exactly as before.
        rb = self.res_beta[zone]
        mean_ror = float(rb.get("mean_ror_mw", 0.0))
        mean_sw = float(rb.get("mean_sw_mw", rb["mean_res_mw"]))
        rg = res_growth if ror_growth is None else ror_growth
        ror_part = np.zeros(n)
        if mean_ror > 0 and self.ror_shape.get(zone) is not None:
            ror_part = np.resize(self.ror_shape[zone], n) * mean_ror * rg
        sw_clim = np.resize(self.sw_shape[zone], n) if self.sw_shape.get(zone) is not None else own
        _need = {"wind_speed_ms", "cloud_cover_pct"}
        if zw is not None and zone in self.res_coef and _need <= set(zw.columns):
            # own-weather RES: the zone's fitted response to ITS wind and ITS clearness. A Danish wind lull
            # is now a Danish event, instead of a rescaling of the French renewable anomaly.
            w = zw["wind_speed_ms"].reindex(idx).to_numpy(float)
            c = zw["cloud_cover_pct"].reindex(idx).to_numpy(float)
            res = _res_design(w, c, sw_clim) @ np.array(self.res_coef[zone])
            # CLIP BEFORE NORMALISING. An OLS response goes negative in low-wind, high-cloud hours, and the
            # caller clips the frame at zero on the way out. Normalising the UNCLIPPED series and clipping
            # afterwards therefore delivers a mean ABOVE the intended level by exactly the negative mass —
            # measured, that inflated neighbour RES by +10 to +14 % system-wide and up to +21 % in
            # IT-North, whose fit is the poorest, which then halved projected prices. Clipping first makes
            # the normalisation act on the series actually delivered.
            res = np.clip(res, 0.0, None)
            mean_fit = float(np.nanmean(res)) or 1.0
            # re-anchor on the fitted mean level, then apply capacity growth: the regression sets the SHAPE
            # and the anomaly, the scenario sets the level.
            res = res / mean_fit * mean_sw * res_growth + ror_part
        else:
            draw = fr_res_cf.reindex(idx).to_numpy(float)
            draw = draw / (np.nanmean(draw) or 1.0)                # unit-mean FR draw shape
            clim = np.resize(self.fr_res_clim, n)
            coher = np.clip(np.divide(draw, clim, out=np.ones_like(draw), where=clim > 1e-6), 0.0, 3.0)
            res = sw_clim * coher * mean_sw * res_growth + ror_part
        return pd.DataFrame({"timestamp_utc": idx, "load_mw": np.clip(load, 0, None),
                             "musttake_res_mw": np.clip(res, 0, None)}).dropna().reset_index(drop=True)


# -------------------------------------------------- assemble the hook payload ----------------------------
_DRAW_ZW_CACHE: dict = {}


def _draw_zone_weather(config, realization: int = 0) -> dict:
    """{zone: DataFrame} of the DRAW's own-zone weather, read once per (process, realization).

    Reads the same cube `fr_draw` used, via `POWERSIM_WEATHER_CUBE` when a Monte-Carlo draw redirects it,
    so the neighbours' weather and France's come from one realization rather than two."""
    if realization in _DRAW_ZW_CACHE:
        return _DRAW_ZW_CACHE[realization]
    out: dict = {}
    try:
        from powersim_core.weather_cube import load_zone_weather
        path = config.resolve(config.section("data")["weathergen_output"])             if "weathergen_output" in config.section("data") else             _ROOT / "weathergen" / "output" / "simulation.nc"
        out = load_zone_weather(str(path),
                                ["temperature_c", "wind_speed_ms", "cloud_cover_pct"],
                                realization=realization)
        out.pop("FR", None)                       # FR comes from the exact steps iii/iv, not this proxy
    except Exception:                             # noqa: BLE001 — a missing cube must degrade, not crash
        out = {}
    _DRAW_ZW_CACHE[realization] = out
    return out


def all_weather_shapes(config, target_year: int, realization: int = 0, nb_model: NeighbourWeatherModel | None = None,
                       growth: dict | None = None) -> dict:
    """{zone: net-load df} for the projection's `weather_shapes` hook, from one coherent FR weathergen draw:
    FR exact (demand+RES models), neighbours reduced-form (fit `nb_model` once and pass it). `growth` =
    {zone: (load_growth, res_growth)} applies the structural level (e.g. TYNDP factors, #76) on top of the
    weather-driven shape; default 1.0. Zones the neighbour model couldn't fit are omitted → the projection
    falls back to their reference-year shape for those."""
    draw = fr_draw(realization)
    out = {"FR": fr_shape(target_year, draw=draw)}
    if nb_model is not None:
        temp = draw["temp_nat"]
        res_cf = draw["res_mw"]                              # FR national RES production → the neighbour shape
        # THIS draw's own-zone weather, straight off the cube the draw was generated from. {} on a
        # France-only cube, in which case every zone falls back to the FR proxy exactly as before.
        zwx = _draw_zone_weather(config, realization)
        for z in nb_model.load_coef:
            # (load, res) historically; (load, res, run-of-river) since the must-take split. Unpacked
            # tolerantly so a caller still passing the 2-tuple keeps working.
            _g = tuple((growth or {}).get(z, (1.0, 1.0)))
            lg, rg = _g[0], _g[1]
            _g = (growth or {}).get(z, (1.0, 1.0))
            s = nb_model.shape(z, target_year, temp, res_cf, load_growth=lg, res_growth=rg,
                               zone_weather=zwx.get(z),
                               ror_growth=(_g[2] if len(_g) > 2 else None))
            if s is not None and not s.empty:
                out[z] = s
    return out


def _neighbour_model(config, fit_year: int = 2019) -> NeighbourWeatherModel:
    """Fit (and cache per process) the reduced-form neighbour model once — the OLS over `fit_year` history."""
    if fit_year not in _NB_MODEL_CACHE:
        _NB_MODEL_CACHE[fit_year] = NeighbourWeatherModel.fit(config, year=fit_year)
    return _NB_MODEL_CACHE[fit_year]


def default_weather_provider(config, target_year: int, draw: int = 0, ref_year: int = 2019,
                             fit_year: int = 2019) -> dict:
    """Top-level, picklable ``weather_provider(config, year, draw) -> {zone: net-load df}`` — the DEFAULT
    projection weather source (replaces the reshape-2019 path).

    FR is exact (demand+RES models on weathergen realization ``draw``); neighbours are the reduced-form
    stand-in, levelled to the target year by their TYNDP demand/RES factors (so the plan capacities set the
    level and the FR draw sets the shape). Everything is cached per process, so the FR draw and the neighbour
    fit are each paid once regardless of how many years/draws are requested. Picklable → usable as the
    ``run_ensemble`` weather_provider across the process pool."""
    from .tyndp import load_tyndp, tyndp_factors
    nb = _neighbour_model(config, fit_year)
    tyndp = load_tyndp(config.resolve(config.section("assumptions")["workbook"]))
    k = target_year - ref_year
    growth = {}
    for z in nb.load_coef:
        tf = tyndp_factors(tyndp, z, target_year, ref_year) if tyndp else None
        lg = tf["demand"] if (tf and tf.get("demand")) else (1 + _G_FALLBACK["demand"]) ** k
        rg = tf["res"] if (tf and tf.get("res")) else (1 + _G_FALLBACK["res"]) ** k
        # run-of-river follows the HYDRO capacity trajectory, not the solar+wind one
        hg = (tf or {}).get("cap", {}).get("hydro_ror")
        growth[z] = (lg, rg, hg if hg else 1.0)
    return all_weather_shapes(config, target_year, realization=draw, nb_model=nb, growth=growth)
