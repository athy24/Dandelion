"""Assemble REFERENCE_WEATHER_20Y: for each projected year y, the weather of draw d(y) of the year map.

The map (archive/reference_20y/ref_weather_map.json, chosen by scripts/select_reference_weather.py on one completed
ensemble) names, year by year, a draw whose own run priced that year at the ensemble's centre. This builds the
weather half of the REFERENCE scenario from it; scripts/build_reference_availability.py builds the availability half
and scripts/run_reference_20y.py the French demand/RES half (see scripts/reference_scenario.py for why three).

WHAT THE CUBE IS USED FOR. In a reference run France's weather-driven series do not come from this cube (they are
draw d(y)'s own frames); the dispatch reads it for the neighbours' hourly temperature, 10 m wind and cloud cover
(`weather_shapes._draw_zone_weather`). It is NOT a physically sampled 20-year sequence -- consecutive years come
from different draws, so year-to-year persistence is broken by construction -- and anything that depends on
multi-year weather memory should be read from the ensemble, not from here.

THE CUBES ARE REGENERATED, AND PROVEN TO BE THE DRAWS'. A Monte-Carlo draw deletes its cube (854 MB) once priced,
but `generate_cube` is seeded `SeedSequence([master_seed, draw])`, so the cube is reproducible -- IF the weather
model is the one the ensemble ran. So: the model on disk is compared with the ensemble's FINGERPRINT.json first
(any difference refuses), and each regenerated cube must then match two records the draw left behind: its exact
byte size (the sidecar of the driver caches the draw wrote, snapshotted by run_reference_20y) and its per-year
national weather summary (reports/<ensemble>/draw_<d>/weather_summary.csv, every column, every year).

THE SEAM. 31 December and 1 January of consecutive years come from unrelated draws, so the join is a step. It is
removed to first order: for each station and variable the step is halved into each side and tapered linearly to
zero over `SEAM_HOURS`, then bounded variables are re-clipped. A boundary between two years of the SAME draw is
continuous weather and is left untouched. The step before and the residual after correction
are printed at build time. Those 72 hours either side of 19 year boundaries are the one place a reference run's
neighbour weather differs from draw d(y)'s own.

RESTARTABLE, AND CHEAP TO ADJUST. Regenerated cubes stay in scratchpad/refweather (854 MB each), reused only when
their attributes name this model, seed and draw AND they verified -- the verification is recorded beside the cube
(`cube_<d>.verified.json`, keyed on its size and mtime) so it is not recomputed. A year map adjusted after the
baseline gate (adjust_reference_map.py) then regenerates only the draws it newly names; cubes of draws the map no
longer names are deleted, and run_sensitivities deletes the rest once the whole set is complete. The
output is written through a temp name. If archive/REFERENCE_WEATHER_20Y.nc already verifies against the map it is
left alone; a canonical cube of another ensemble is COPIED aside first (run_all.ps1 refuses to start without one).

    python -u -X utf8 -W ignore scripts/build_reference_weather.py [--force]
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import reference_scenario as rs  # noqa: E402

WORK = rs.REPO / "scratchpad" / "refweather"
SEAM_HOURS = 72                       # taper each side of a year boundary over three days
COORDS = ("latitude", "longitude", "elevation", "lst_offset_h", "zone")

#: variables that cannot leave their physical range after a seam correction
BOUNDS = {"cloud_cover_pct": (0.0, 100.0), "humidity_pct": (0.0, 100.0),
          "precip_1h_mm": (0.0, None), "wind_speed_ms": (0.0, None),
          "wind_speed_100m_ms": (0.0, None)}


def year_spans(times: pd.DatetimeIndex, years: list[int]) -> dict[int, tuple[int, int]]:
    """year -> [start, stop) row span of the shared hourly index; each year must be present and contiguous."""
    yr = times.year.to_numpy()
    spans = {}
    for y in years:
        idx = np.flatnonzero(yr == y)
        if idx.size == 0:
            raise SystemExit(f"year {y} absent from the cube time index")
        if idx[-1] - idx[0] + 1 != idx.size:
            raise SystemExit(f"year {y} is not contiguous in the time index")
        spans[y] = (int(idx[0]), int(idx[-1]) + 1)
    return spans


def taper_seams(buf: np.ndarray, spans: dict[int, tuple[int, int]], seam_hours: int = SEAM_HOURS,
                sel: dict[int, int] | None = None) -> list:
    """Remove the 1-January step in place (module docstring). -> [(year, max |step| per variable)].
    With `sel`, a boundary whose two years come from the same draw is continuous weather and is skipped."""
    ramp = np.arange(1, seam_hours + 1) / seam_hours         # 1/W .. 1, rising toward the boundary
    report = []
    n_t = buf.shape[0]
    for y in sorted(spans)[1:]:
        if sel is not None and sel.get(y - 1) == sel.get(y):
            continue
        i0 = spans[y][0]
        if i0 - seam_hours < 0 or i0 + seam_hours > n_t:
            continue
        step = buf[i0] - buf[i0 - 1]                          # (station, variable)
        report.append((y, np.abs(step).max(axis=0)))
        buf[i0 - seam_hours:i0] += 0.5 * step[None, :, :] * ramp[:, None, None]
        buf[i0:i0 + seam_hours] -= 0.5 * step[None, :, :] * ramp[::-1][:, None, None]
    return report


def clip_bounds(buf: np.ndarray, variables: list[str]) -> None:
    for j, v in enumerate(variables):
        lo, hi = BOUNDS.get(v, (None, None))
        if lo is not None or hi is not None:
            buf[:, :, j] = np.clip(buf[:, :, j], lo, hi)


def recorded_cube_size(d: int, fp: str) -> int | None:
    """The byte size of draw d's cube, from the sidecar of the driver caches it wrote (snapshot first, then lake)."""
    import run_reference_20y as rr

    from powersim_core import lake
    for layer, dataset in rr.DRIVER_TABLES:
        for side in (rr._snap_part(layer, dataset, d).with_name("source_cube.json"),
                     lake.table_path(layer, dataset, realization=d).with_name("source_cube.json")):
            if side.exists() and rr._sidecar_names_draw(side, fp, d):
                return int(json.loads(side.read_text(encoding="utf-8"))["size"])
    return None


def verify_regenerated(cube: Path, d: int, fp: str, mc: Path | None = None) -> list[str]:
    """Problems that make `cube` not the cube draw d of ensemble fp priced on (empty = it is)."""
    from mc_weather import weather_summary
    mc = Path(mc or rs.MC_OUT)
    problems = []
    size = recorded_cube_size(d, fp)
    if size is None:
        problems.append(f"draw {d}: no recorded cube size to compare with")
    elif cube.stat().st_size != size:
        problems.append(f"draw {d}: regenerated cube is {cube.stat().st_size} bytes, the draw's was {size}")
    rec_f = Path(mc) / f"draw_{d:03d}" / "weather_summary.csv"
    if not rec_f.exists():
        return problems + [f"draw {d}: {rec_f} absent"]
    rec = pd.read_csv(rec_f).set_index("year")
    now = weather_summary(cube).set_index("year")
    if list(now.index) != list(rec.index) or set(now.columns) != set(rec.columns):
        return problems + [f"draw {d}: weather summary years or columns differ from the draw's"]
    num = [c for c in rec.columns if pd.api.types.is_numeric_dtype(rec[c])]
    diff = (now[num].astype(float) - rec[num].astype(float)).abs()
    tol = 1e-9 * np.maximum(1.0, rec[num].abs().astype(float))
    bad = (diff > tol).any(axis=0)
    if bad.any():
        problems.append(f"draw {d}: weather summary differs from the draw's in {sorted(bad[bad].index)} "
                        f"(max |diff| {float(diff.to_numpy().max()):.3g})")
    return problems


def _cube_ok(cube: Path, d: int, fp: str) -> bool:
    import xarray as xr
    try:
        with xr.open_dataset(cube) as ds:
            return (str(ds.attrs.get("model_fp")) == fp and int(ds.attrs.get("mc_master_seed", -1)) == rs.MASTER_SEED
                    and int(ds.attrs.get("mc_draw", -1)) == d)
    except Exception:                                          # noqa: BLE001 - unreadable = not ours
        return False


def _verified_path(cube: Path) -> Path:
    return cube.with_name(cube.stem + ".verified.json")


def _identity(cube: Path, fp: str, d: int) -> dict:
    st = cube.stat()
    return {"draw": d, "model_fp": fp, "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}


def regenerate(d: int, fp: str) -> Path:
    from mc_weather import generate_cube
    cube = WORK / f"cube_{d:03d}.nc"
    if cube.exists() and not _cube_ok(cube, d, fp):
        print(f"  {cube.name} was not written by model {fp}, seed {rs.MASTER_SEED}, draw {d} - regenerating")
        cube.unlink()
        _verified_path(cube).unlink(missing_ok=True)
    if not cube.exists():
        _verified_path(cube).unlink(missing_ok=True)
        t0 = time.monotonic()
        generate_cube(d, cube, rs.MASTER_SEED, model_fp=fp)
        print(f"  draw {d:2d}: cube regenerated in {time.monotonic() - t0:.0f}s", flush=True)
    vp = _verified_path(cube)
    try:
        if json.loads(vp.read_text(encoding="utf-8")) == _identity(cube, fp, d):
            return cube
    except (OSError, ValueError):
        pass
    problems = verify_regenerated(cube, d, fp)
    if problems:
        raise SystemExit("regenerated weather is not the ensemble's: " + "; ".join(problems))
    vp.write_text(json.dumps(_identity(cube, fp, d)), encoding="utf-8")
    return cube


def prune_cubes(keep: set[int] | None = None) -> int:
    """Delete regenerated cubes (and their verification records) of draws not in `keep` (all when None)."""
    n = 0
    for c in WORK.glob("cube_*.nc"):
        try:
            d = int(c.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        if keep is None or d not in keep:
            _unlink_quietly(c)
            _unlink_quietly(_verified_path(c))
            n += 1
    return n


def build(sel: dict[int, int], meta: dict, force: bool = False) -> bool:
    import run_reference_20y as rr
    import xarray as xr
    fp = meta["model_fingerprint"]
    if not force and not rs.verify_cube(sel, meta):
        print(f"{rs.CUBE_PATH.name} already is the reference weather of this map - nothing to do")
        return False
    incomplete = sorted(d for d in set(sel.values()) if not rs.draw_complete(d, fp))
    if incomplete:
        raise SystemExit(f"draws {incomplete} of the map are not complete under ensemble {fp}")
    rr.require_ensemble_model(fp)
    WORK.mkdir(parents=True, exist_ok=True)
    draws = sorted(set(sel.values()))
    print(f"year -> draw: {sel}  (ensemble {fp}, {len(draws)} draws to regenerate)", flush=True)

    first = regenerate(draws[0], fp)
    with xr.open_dataset(first) as ref:
        times = pd.DatetimeIndex(pd.to_datetime(ref["obs"]["time"].values))
        stations = ref["station"].values.copy()
        variables = [str(v) for v in ref["variable"].values]
        station_coords = {c: ("station", ref[c].values.copy()) for c in COORDS if c in ref.coords}
    n_t, n_s, n_v = len(times), len(stations), len(variables)
    print(f"grid: {n_t} h x {n_s} stations x {n_v} variables")
    spans = year_spans(times, sorted(sel))

    buf_path = WORK / "assembled.npy"
    _unlink_quietly(buf_path)                                 # a buffer left by a killed build is never reused
    buf = np.lib.format.open_memmap(buf_path, mode="w+", dtype=np.float64, shape=(n_t, n_s, n_v))
    filled = np.zeros(n_t, dtype=bool)
    cubes = []
    for d in draws:
        years = sorted(y for y, dd in sel.items() if dd == d)
        cube = first if d == draws[0] else regenerate(d, fp)
        cubes.append(cube)
        with xr.open_dataset(cube) as ds:
            if not (np.array_equal(pd.to_datetime(ds["obs"]["time"].values), times)
                    and np.array_equal(ds["station"].values, stations)
                    and [str(v) for v in ds["variable"].values] == variables
                    and all(np.array_equal(ds[c].values, station_coords[c][1]) for c in station_coords)):
                raise SystemExit(f"draw {d}: cube grid (time, station, variable or station coords) differs")
            da = ds["obs"].transpose("time", "station", "variable")
            for y in years:
                a, b = spans[y]
                buf[a:b] = da.isel(time=slice(a, b)).values       # lazy read: only this year leaves the file
                filled[a:b] = True
        print(f"  draw {d:2d} -> {years}", flush=True)
    if not filled.all():
        raise SystemExit(f"{(~filled).sum()} hours unfilled -- the year map does not cover the cube's horizon")

    report = taper_seams(buf, spans, sel=sel)
    clip_bounds(buf, variables)
    print("\nseam step at 1 Jan 00:00 BEFORE correction (max over stations, per variable):")
    print(f"{'year':<5s} {'prev':>4s} {'next':>4s}  " + " ".join(f"{v[:12]:>12s}" for v in variables))
    for y, mx in report:
        print(f"{y:<5d} {sel[y - 1]:4d} {sel[y]:4d}  " + " ".join(f"{m:12.2f}" for m in mx))
    resid = [np.abs(buf[spans[y][0]] - buf[spans[y][0] - 1]).max(axis=0) for y, _ in report]
    if resid:
        print("max residual step AFTER correction: " +
              "  ".join(f"{v}={m:.3f}" for v, m in zip(variables, np.max(resid, axis=0), strict=True)))

    out = xr.Dataset(
        {"obs": xr.DataArray(buf, dims=("time", "station", "variable"),
                             coords={"time": times, "station": stations, "variable": variables, **station_coords})},
        attrs={"reference_weather": "REFERENCE_WEATHER_20Y",
               "built_from": (f"{rs.MC_OUT.name} ensemble {fp}, draws {meta['draws'][0]}-{meta['draws'][-1]} "
                              f"({len(meta['draws'])}), master_seed {rs.MASTER_SEED}"),
               "model_fp": fp,
               "year_map_verified": 1,
               "map_meta_sha256": rs.file_sha256(rs.META_PATH),
               "selection": str(meta.get("criterion", "")),
               "fr_tol_eur_mwh": float(meta.get("fr_tol_eur_mwh", float("nan"))),
               "year_to_draw": json.dumps({str(y): d for y, d in sorted(sel.items())}),
               "seam_hours": SEAM_HOURS,
               "note": ("Weather half of the REFERENCE scenario (scripts/reference_scenario.py). Consecutive years "
                        "come from DIFFERENT draws, so multi-year weather persistence is broken by construction.")})
    old = rs.CUBE_PATH
    if old.exists():
        keep_aside(old, fp)
    tmp = old.with_name(old.name + ".tmp")                    # a killed write must not leave a truncated reference
    out.to_netcdf(tmp)
    out.close()
    # the Dataset holds the memmap itself: every reference must go before Windows lets the buffer file be deleted
    del out, buf
    gc.collect()
    os.replace(tmp, old)
    problems = rs.verify_cube(sel, meta)
    if problems:
        raise SystemExit("written reference weather does not verify: " + "; ".join(problems))
    _unlink_quietly(buf_path)                                # scratch only: a failed delete must not fail the build
    pruned = prune_cubes(set(draws))                          # kept for an adjustment round; others are not needed
    if pruned:
        print(f"deleted {pruned} regenerated cube(s) of draws the map no longer names")
    print(f"\nwrote {old}  {old.stat().st_size / 1e6:.0f} MB")
    return True


def _unlink_quietly(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except OSError as exc:
        print(f"WARNING: could not delete scratch file {p} ({exc}); delete it by hand")


def keep_aside(old: Path, fp: str) -> Path | None:
    """Copy a canonical cube of ANOTHER ensemble aside before it is replaced. Atomic, and an existing copy is trusted
    only when it has the original's size; otherwise the replacement is refused rather than risk the only copy."""
    try:
        was = str(rs.cube_attrs(old).get("model_fp"))
    except Exception:                                          # noqa: BLE001 - unreadable: still keep a copy
        was = "unreadable"
    if was == fp:
        return None
    keep = old.with_name(f"{old.stem}_{was}{old.suffix}")
    if keep.exists() and keep.stat().st_size == old.stat().st_size:
        return keep
    tmp = keep.with_name(keep.name + ".tmp")
    shutil.copy2(old, tmp)
    os.replace(tmp, keep)
    if keep.stat().st_size != old.stat().st_size:
        raise SystemExit(f"copy of the previous reference weather to {keep.name} is incomplete: not replacing it")
    print(f"previous reference weather (ensemble {was}) copied to {keep.name}")
    return keep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebuild even if the reference already verifies")
    a = ap.parse_args()
    sel, meta = rs.load_map()
    build(sel, meta, a.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
