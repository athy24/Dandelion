"""Canonical weathergen-cube reader (§5) — the single home for cube loading, replacing the 3 duplicate
loaders in demand/res/availability.

The cube is `simulation.nc` (`obs` DataArray over dims time × station × variable, optional ensemble dim).
Reductions provided: national daily temperature + annual wetness (availability/derating side) and a tidy
per-station frame (demand/res side). Logic is byte-for-byte the previous per-model loaders so the golden
baseline is preserved on rewiring.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_ENSEMBLE_DIMS = ("realization", "member", "draw", "ensemble")


def effective_cube_path(path):
    """The cube path actually read, after the `POWERSIM_WEATHER_CUBE` override.

    Cache-freshness checks in demand/res compare a cached table's mtime against the cube's. They must
    compare against the cube that will REALLY be read: resolving the configured path instead means a
    Monte-Carlo draw pointed elsewhere by the override sees a cache built from a different draw's weather
    and silently declares it fresh. That is a wrong-answer bug, not a slow one."""
    import os
    from pathlib import Path
    return Path(os.environ.get("POWERSIM_WEATHER_CUBE") or path)


def open_cube(path):
    """Open the weathergen cube, honouring the `POWERSIM_WEATHER_CUBE` override.

    Four configs name this file (demand, res, availability, dispatch) and a Monte-Carlo draw needs all
    four to read the SAME per-draw cube. Overriding one env var here — read at call time, never cached —
    redirects every consumer at once, which is what makes a draw coherent across steps iii/iv/v/vi.
    Unset (the normal case) the caller's configured path is used unchanged."""
    import os
    from pathlib import Path

    import xarray as xr
    override = os.environ.get("POWERSIM_WEATHER_CUBE")
    if override:
        path = override
    if not Path(path).exists():
        raise FileNotFoundError(f"weathergen cube not found: {path}")
    return xr.open_dataset(path)


def cube_variables(path) -> list[str]:
    """The cube's variable names (cheap — reads only the coordinate)."""
    ds = open_cube(path)
    try:
        return [str(v) for v in ds["obs"]["variable"].values]
    finally:
        ds.close()


def _select_realization(ds, realization: int):
    for dim in _ENSEMBLE_DIMS:
        if dim in ds.dims:
            return ds.isel({dim: realization % ds.sizes[dim]})
    return ds


def zone_map(path) -> dict[str, list[str]]:
    """{zone: [station_id, ...]} from the cube's `zone` coordinate, or {} on a France-only cube.

    Cubes generated before the neighbour extension carry no `zone` coord; callers must degrade rather than
    fail, because the France-only cube stays valid for every FR-only consumer (steps iii and iv)."""
    ds = open_cube(path)
    try:
        if "zone" not in ds.coords:
            return {}
        z = [str(v) for v in ds["zone"].values]
        st = [str(v) for v in ds["obs"]["station"].values]
    finally:
        ds.close()
    out: dict[str, list[str]] = {}
    for zz, ss in zip(z, st):
        out.setdefault(zz, []).append(ss)
    return out


def load_zone_weather(path, variables: list[str], realization: int = 0) -> dict:
    """{zone: DataFrame[timestamp_utc, <variables>]} — each zone's own station-mean hourly weather.

    This is what replaces "regress every neighbour on FRENCH temperature". A zone's demand response and
    its renewable output are driven by ITS weather; the cross-border correlation that matters is then
    whatever the weathergen copula produced between the stations, rather than a correlation of 1 imposed
    by construction."""
    import numpy as np
    import pandas as pd

    ds = _select_realization(open_cube(path), realization)
    try:
        da = ds["obs"]
        time = pd.to_datetime(da["time"].values)
        if time.tz is None:
            time = time.tz_localize("UTC")
        vmap = {str(v): i for i, v in enumerate(da["variable"].values)}
        stations = [str(v) for v in da["station"].values]
        zones = [str(v) for v in ds["zone"].values] if "zone" in ds.coords else ["FR"] * len(stations)
        arr = da.transpose("time", "station", "variable").values
    finally:
        ds.close()

    idx: dict[str, list[int]] = {}
    for i, z in enumerate(zones):
        idx.setdefault(z, []).append(i)
    out = {}
    for z, cols in idx.items():
        d = {}
        for v in variables:
            if v in vmap:
                d[v] = np.nanmean(arr[:, cols, vmap[v]], axis=1)
        if d:
            out[z] = pd.DataFrame(d, index=time).rename_axis("timestamp_utc")
    return out


def _zone_cols(ds, zone: str | None) -> list[int] | None:
    """Station indices belonging to `zone`, or None to keep every station.

    THE CUBE IS NO LONGER FRANCE-ONLY. Every reduction below used to average all stations because all
    stations were French; with the neighbour extension that silently turns "France's national temperature"
    into a European one — Madrid at 15.9 °C and Warsaw at 8.8 °C averaged into a 13.2 °C country. Callers
    that mean FRANCE must say so, and on a France-only cube (no `zone` coord) this is a no-op, so the
    pre-extension behaviour is preserved exactly."""
    if zone is None or "zone" not in ds.coords:
        return None
    z = [str(v) for v in ds["zone"].values]
    cols = [i for i, zz in enumerate(z) if zz == zone]
    return cols or None


def load_national_weather(path, realization: int = 0, zone: str | None = "FR") -> tuple[pd.Series, dict[int, float]]:
    """→ (national daily-mean temperature °C, wetness_by_year = annual precip / mean annual precip).

    `zone` defaults to FR: this feeds French nuclear thermal derating and French reservoir wetness, and
    must not be a European average. Pass `zone=None` for a whole-cube mean."""
    ds = _select_realization(open_cube(path), realization)
    try:
        da = ds["obs"]
        time = pd.to_datetime(da["time"].values)
        if time.tz is None:
            time = time.tz_localize("UTC")
        vmap = {str(v): i for i, v in enumerate(da["variable"].values)}
        arr = da.transpose("time", "station", "variable").values
        _c = _zone_cols(ds, zone)
        _sel = slice(None) if _c is None else _c
        temp = np.nanmean(arr[:, _sel, vmap["temperature_c"]], axis=1)
        precip = np.nanmean(arr[:, _sel, vmap["precip_1h_mm"]], axis=1)
    finally:
        ds.close()
    temp_daily = pd.Series(temp, index=time).resample("1D").mean()
    annual = pd.Series(precip, index=time).resample("1YE").sum()
    mean_annual = annual.mean()
    wetness = {int(ts.year): float(v / mean_annual) if mean_annual else 1.0 for ts, v in annual.items()}
    return temp_daily, wetness


def load_station_tidy(path, variables, realization: int = 0,
                      zone: str | None = "FR") -> tuple[pd.DataFrame, pd.DataFrame]:
    """→ (tidy [timestamp_utc, station_id, <variables>], stations metadata) — demand/res side.

    `zone` defaults to FR because both consumers are French: `res_model` builds NATIONAL PV and wind from
    these stations and `demand_model` builds French temperature features. Left unfiltered on the extended
    cube they would compute France from 76 European stations. A France-only cube has no `zone` coord, so
    this is a no-op there and the pre-extension behaviour is exact."""
    ds = _select_realization(open_cube(path), realization)
    try:
        da = ds["obs"]
        time = pd.to_datetime(da["time"].values)
        if time.tz is None:
            time = time.tz_localize("UTC")
        var_names = [str(v) for v in da["variable"].values]
        _c = _zone_cols(ds, zone)
        keep = list(range(len(da["station"].values))) if _c is None else _c
        stn = [str(s) for s in da["station"].values[keep]]
        arr = da.transpose("time", "station", "variable").values[:, keep, :]
        stations = pd.DataFrame({"station_id": stn,
                                 "latitude": np.asarray(ds["latitude"].values, float)[keep],
                                 "longitude": np.asarray(ds["longitude"].values, float)[keep],
                                 "altitude": np.asarray(ds["elevation"].values, float)[keep]})
    finally:
        ds.close()
    vmap = {v: i for i, v in enumerate(var_names)}
    frames = []
    for j, sid in enumerate(stn):
        block = pd.DataFrame({"timestamp_utc": time, "station_id": sid})
        for v in variables:
            block[v] = arr[:, j, vmap[v]] if v in vmap else np.nan
        frames.append(block)
    return pd.concat(frames, ignore_index=True), stations


def cube_identity(path) -> dict:
    """What a cache must remember to know it was built from THIS cube: resolved path, size, mtime (ns)."""
    from pathlib import Path
    p = Path(path).resolve()
    st = p.stat()
    return {"path": str(p), "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}


def cache_is_fresh(cache_path, cube_path) -> bool:
    """A lake cache is fresh only if its sidecar names the cube that will really be read now.

    The previous test — cache newer than cube, keyed on the realization number alone — let three different cubes
    that all use realization 0 (the default cube, REFERENCE_WEATHER_20Y, Monte-Carlo draw 0) hand each other
    their drivers: whichever was written last looked fresh for the others, and the golden baseline captured a
    mixture of two of them. Partitions written before the sidecar existed carry none and are rebuilt."""
    import json
    from pathlib import Path
    cache_path, cube_path = Path(cache_path), Path(cube_path)
    side = cache_path.with_name("source_cube.json")
    if not (cache_path.exists() and cube_path.exists() and side.exists()):
        return False
    try:
        return json.loads(side.read_text(encoding="utf-8")) == cube_identity(cube_path)
    except Exception:                                                  # noqa: BLE001 - unreadable = not fresh
        return False


def record_cache_source(cache_path, cube_path) -> None:
    """Write the sidecar that `cache_is_fresh` checks, next to the cache partition it describes."""
    import json
    from pathlib import Path
    Path(cache_path).with_name("source_cube.json").write_text(json.dumps(cube_identity(cube_path)), encoding="utf-8")

