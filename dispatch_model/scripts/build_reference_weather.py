"""Assemble REFERENCE_WEATHER_20Y: for each projected year, the weather of the MOST TYPICAL draw.

For every year of the horizon the 50-draw ensemble gives 50 French annual mean spot prices. The draw whose
price for that year sits closest to the ensemble mean is the "most typical" realisation OF THAT YEAR, and
its weather for that year is taken. Twenty years, sixteen distinct draws, worst deviation 0.47 EUR/MWh and
most under 0.10 -- so each year of this trajectory prices, on its own, essentially at the ensemble mean.

WHAT THIS IS AND IS NOT. It is a single deterministic weather trajectory that reproduces the ensemble's
CENTRAL price path year by year, which is what a sensitivity needs: change one assumption, hold weather
fixed, read the difference. It is NOT a physically sampled 20-year sequence -- consecutive years come from
different draws, so year-to-year persistence (a dry winter following a dry autumn, reservoir carry-over
across a New Year) is broken by construction. Anything that depends on multi-year weather memory should be
read from the ensemble, not from here.

THE SEAM. Consecutive years come from different simulations, so 31 December and 1 January are unrelated
draws and the join is a step. It is removed to first order: for each station and variable the step is
halved into each side and tapered linearly to zero over `SEAM_HOURS`, so the two sides meet continuously
without inventing data. Bounded variables are re-clipped afterwards. Higher-order discontinuity -- a storm
ending where a calm begins -- is inherent to the construction and is left alone; it is reported, not hidden.

MEASURED, on the built cube. Steps before correction ran 8-18 degC and up to 100 points of cloud cover;
the natural year boundary WITHIN one draw (2043->2044, both draw 37) is 1.83 degC, which is the scale the
artificial ones had to be brought down to. Residual after correction: 0.000 for every variable. The taper
touches 2736 of 175200 hours (1.56 %), and of the sixteen extremes in the finished cube exactly one falls
inside a taper window -- precipitation 60.19 mm/h against the generator's own 60.00 ceiling, i.e. the taper
added 0.19 mm to an hour already at the cap. Left as is: it is 0.3 % on one hour of one station, and
precipitation enters only through annual sums (hydro wetness).

The cubes themselves were deleted after each draw (854 MB each), but `generate_cube` is seeded
`SeedSequence([master_seed, draw])`, so every draw is reproducible exactly. Only the 16 needed are rebuilt,
one at a time, and each is deleted once its year(s) have been copied out.

THE MODEL (2026-09-14). A year map is a selection made on ONE ensemble's prices, and a cube is ONE weather
model's draw. Both used to be taken on trust: a cube left in `scratchpad/refweather/` was reused whatever wrote
it, and nothing tied `ref_weather_map.json` to the ensemble it was chosen from. So every draw the map names must
be complete under the model on disk (`--allow-unverified-map` overrides), a cube is reused only when its
attributes name this model, seed and draw, and the output is written through a temp name and stamped `model_fp`.

    python -u -X utf8 -W ignore scripts/build_reference_weather.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mc_weather import generate_cube  # noqa: E402

REPO = _HERE.parent
REF_CUBE = REPO.parent / "weathergen" / "output" / "simulation.nc"
OUT = REPO.parent / "archive" / "REFERENCE_WEATHER_20Y.nc"
WORK = REPO / "scratchpad" / "refweather"
WORK.mkdir(parents=True, exist_ok=True)
MASTER_SEED = 20260629
SEAM_HOURS = 72                       # taper each side of a year boundary over three days

#: variables that cannot leave their physical range after a seam correction
BOUNDS = {"cloud_cover_pct": (0.0, 100.0), "humidity_pct": (0.0, 100.0),
          "precip_1h_mm": (0.0, None), "wind_speed_ms": (0.0, None),
          "wind_speed_100m_ms": (0.0, None)}

_ap = argparse.ArgumentParser()
_ap.add_argument("--allow-unverified-map", action="store_true",
                 help="build even when a draw the year map names is not complete under the model on disk")
_args = _ap.parse_args()
from model_fingerprint import digest  # noqa: E402

FP = digest({"master_seed": MASTER_SEED, "n_weeks": None})              # run_montecarlo defaults

sel = {int(k): int(v) for k, v in
       json.loads((REPO / "scratchpad" / "ref_weather_map.json").read_text(encoding="utf-8")).items()}
print(f"year -> draw: {sel}  (model {FP})")


def _complete_under_model(d: int) -> bool:
    m = REPO / "reports" / "mc50" / f"draw_{d:03d}" / ".draw_complete"
    return m.exists() and m.read_text(encoding="utf-8").strip() == FP


# THE MAP MUST BE THIS MODEL'S SELECTION. It names, year by year, the draw that priced closest to the ensemble mean;
# chosen on another model's ensemble it names prices that no longer exist. A necessary condition, checked: every
# draw it names is complete under this model.
_unverified = sorted({d for d in sel.values() if not _complete_under_model(d)})
if _unverified:
    _msg = f"draws {_unverified} of the year map are not complete under model {FP}"
    if not _args.allow_unverified_map:
        raise SystemExit(_msg + ": the map was selected on another ensemble (re-select it, or pass "
                                "--allow-unverified-map)")
    print("WARNING: " + _msg)

ref = xr.open_dataset(REF_CUBE)
times = pd.to_datetime(ref["obs"]["time"].values)
stations = ref["station"].values.copy()
variables = [str(v) for v in ref["variable"].values]
station_coords = {c: ("station", ref[c].values.copy())
                  for c in ("latitude", "longitude", "elevation", "lst_offset_h", "zone")
                  if c in ref.coords}
n_t, n_s, n_v = len(times), len(stations), len(variables)
ref.close()
print(f"grid: {n_t} h x {n_s} stations x {n_v} variables")

# year -> contiguous row span, taken from the shared time index (identical across draws, so the spans align
# exactly and a year lifted from draw A lands on the same calendar hours as one lifted from draw B)
yr = times.year.to_numpy()
spans = {}
for y in sorted(sel):
    idx = np.flatnonzero(yr == y)
    if idx.size == 0:
        raise SystemExit(f"year {y} absent from the cube time index")
    if idx[-1] - idx[0] + 1 != idx.size:
        raise SystemExit(f"year {y} is not contiguous in the time index")
    spans[y] = (int(idx[0]), int(idx[-1]) + 1)

buf = np.lib.format.open_memmap(WORK / "assembled.npy", mode="w+",
                                dtype=np.float64, shape=(n_t, n_s, n_v))
filled = np.zeros(n_t, dtype=bool)

for draw in sorted(set(sel.values())):
    years = sorted(y for y, d in sel.items() if d == draw)
    cube = WORK / f"cube_{draw:03d}.nc"
    t0 = time.monotonic()
    if cube.exists():                                    # reused only if this model, seed and draw wrote it
        try:
            with xr.open_dataset(cube) as _ds:
                _ok = (str(_ds.attrs.get("model_fp")) == FP
                       and int(_ds.attrs.get("mc_master_seed", -1)) == MASTER_SEED
                       and int(_ds.attrs.get("mc_draw", -1)) == draw)
        except Exception:                                # noqa: BLE001 - unreadable = not ours
            _ok = False
        if not _ok:
            print(f"  {cube.name} was not written by model {FP}, seed {MASTER_SEED}, draw {draw} - regenerating")
            cube.unlink(missing_ok=True)
    if not cube.exists():
        generate_cube(draw, cube, MASTER_SEED, model_fp=FP)
    ds = xr.open_dataset(cube)
    da = ds["obs"].transpose("time", "station", "variable")
    if da.sizes["time"] != n_t:
        raise SystemExit(f"draw {draw}: {da.sizes['time']} hours, reference cube has {n_t}")
    if [str(v) for v in ds["variable"].values] != variables:
        raise SystemExit(f"draw {draw}: variable order differs from the reference cube")
    for y in years:
        a, b = spans[y]
        buf[a:b] = da.isel(time=slice(a, b)).values      # lazy read: only this year leaves the file
        filled[a:b] = True
    ds.close()
    cube.unlink(missing_ok=True)                         # 854 MB each; regenerable from the seed
    print(f"  draw {draw:2d} -> {years}  ({time.monotonic() - t0:.0f}s)", flush=True)

if not filled.all():
    raise SystemExit(f"{(~filled).sum()} hours unfilled -- the year map does not cover the horizon")

# ---- seams -------------------------------------------------------------------------------------------
ramp = np.arange(1, SEAM_HOURS + 1) / SEAM_HOURS         # 1/W .. 1, rising toward the boundary
report = []
for y in sorted(sel)[1:]:
    i0 = spans[y][0]
    if i0 - SEAM_HOURS < 0 or i0 + SEAM_HOURS > n_t:
        continue
    step = buf[i0] - buf[i0 - 1]                         # (station, variable)
    report.append((y, sel[y - 1], sel[y], np.abs(step).max(axis=0)))
    buf[i0 - SEAM_HOURS:i0] += 0.5 * step[None, :, :] * ramp[:, None, None]
    buf[i0:i0 + SEAM_HOURS] -= 0.5 * step[None, :, :] * ramp[::-1][:, None, None]

for j, v in enumerate(variables):
    lo, hi = BOUNDS.get(v, (None, None))
    if lo is not None or hi is not None:
        buf[:, :, j] = np.clip(buf[:, :, j], lo, hi)

print("")
print("seam step at 1 Jan 00:00 BEFORE correction (max over stations, per variable):")
print(f"{'year':<5s} {'prev':>4s} {'next':>4s}  " + " ".join(f"{v[:12]:>12s}" for v in variables))
for y, dprev, dnext, mx in report:
    print(f"{y:<5d} {dprev:4d} {dnext:4d}  " + " ".join(f"{m:12.2f}" for m in mx))
resid = []
for y in sorted(sel)[1:]:
    i0 = spans[y][0]
    if i0 - SEAM_HOURS >= 0 and i0 + SEAM_HOURS <= n_t:
        resid.append(np.abs(buf[i0] - buf[i0 - 1]).max(axis=0))
print("max residual step AFTER correction: " +
      "  ".join(f"{v}={m:.3f}" for v, m in zip(variables, np.max(resid, axis=0))))

out = xr.Dataset(
    {"obs": xr.DataArray(buf, dims=("time", "station", "variable"),
                         coords={"time": times, "station": stations, "variable": variables,
                                 **station_coords})},
    attrs={"reference_weather": "REFERENCE_WEATHER_20Y",
           "built_from": "mc50 ensemble, 50 draws, master_seed 20260629",
           "model_fp": FP,
           "year_map_verified": int(not _unverified),
           "selection": "per year, the draw whose FR annual mean spot is closest to the ensemble mean",
           "year_to_draw": json.dumps(sel),
           "seam_hours": SEAM_HOURS,
           "note": ("Deterministic central-weather trajectory for sensitivity runs. Consecutive years come "
                    "from DIFFERENT draws, so multi-year weather persistence is broken by construction.")})
OUT.parent.mkdir(parents=True, exist_ok=True)
tmp = OUT.with_name(OUT.name + ".tmp")                   # a killed write must not leave a newer, truncated reference
out.to_netcdf(tmp)
out.close()
os.replace(tmp, OUT)
del buf
print("")
print(f"wrote {OUT}  {OUT.stat().st_size/1e6:.0f} MB")
