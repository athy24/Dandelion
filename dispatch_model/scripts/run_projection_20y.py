"""Run the projection horizon and persist PRICES, VOLUMES and FLOWS per year.

`project_trajectory` returns a price summary, which is all the gate ever needed. Revenues and congestion
rents need the volume each price was paid on, so this runner drives `project_year` directly with a `sink`
and `DISPATCH_CAPTURE_DISPATCH=1`, writing one parquet set per year:

    <out>/prices_<year>.parquet     hourly SMC and post-markup spot, per zone
    <out>/dispatch_<year>.parquet   hourly MW per (zone, tech), incl. res / ens / dump / storage
    <out>/flows_<year>.parquet      hourly net MW per directed border

The preload is paid once for the whole horizon (~4 min); each year is then ~1 min. Re-running skips years
whose parquet already exists, so an interrupted horizon resumes.

**Availability follows the weather** (the standalone completion of #160): before the preload, step v is
re-projected on the AMBIENT weather cube (`POWERSIM_WEATHER_CUBE` if set, else the shared weathergen
output — the same cube the demand/RES weather shapes read) into the lake layer
`availability_mc/wdraw=<draw>` with `draw_ids=[<draw>]`, and selected via `DISPATCH_AVAIL_WDRAW` — the
exact mechanism `run_montecarlo.py` uses, under which this block is a no-op (its env var is already
set and step v already ran on the draw's own cube). Without it the canonical shared `availability`
layer is used, whose outage streams ignore `--draw` and whose weather-coupled channels (thermal
derating, reservoir wetness) were projected once on the default cube. `--no-avail-weather` restores
that historic behaviour; `--refresh-avail` forces a re-projection when the ambient cube changed (the
lake partition keeps no record of which cube produced it). The partition is reused otherwise, so
resumed horizons do not pay step v twice.

    python -u -X utf8 -W ignore scripts/run_projection_20y.py [--start 2027] [--end 2046] [--draw 0]

This writes nothing to the lake and nothing to reports/ — only to the output directory given.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_ROOT = Path(__file__).resolve().parents[2]                    # …/PriceModeling (repo root)

import pandas as pd                                                            # noqa: E402

from powersim_core.progress import Progress                                    # noqa: E402
from dispatch_model.config import load_config                                  # noqa: E402
from dispatch_model.rolling.projection import _preload, project_year           # noqa: E402


def _ensure_step_v_availability(draw: int, refresh: bool = False) -> None:
    """Make `availability_mc/wdraw=<draw>` exist, projected by step v on the AMBIENT weather cube — the
    same cube the demand/RES weather shapes read, so availability and net load see the same weather.

    Mirrors `run_montecarlo.run_draw`'s step-v invocation (subprocess, so the availability engine's
    imports never load into this process). Reused when the partition exists (resumable, like the year
    parquets); `refresh` forces a re-projection — the lake carries no provenance of which cube
    produced a partition, so a changed ambient cube needs the caller to say so."""
    from powersim_core import lake
    if not refresh:
        try:
            lake.read_table("availability_mc", "availability_by_tech", wdraw=draw)
            print(f"[20y] step-v availability: reusing availability_mc/wdraw={draw} "
                  f"(--refresh-avail to re-project on the current cube)", flush=True)
            return
        except (FileNotFoundError, KeyError, ValueError, OSError):
            pass
    cube = os.environ.get("POWERSIM_WEATHER_CUBE",
                          str(_ROOT / "weathergen" / "output" / "simulation.nc"))
    # The availability engine used to read the cube at ensemble member 0 unconditionally — its weather
    # wrapper passed no realization index. On today's single-member cube that is invisible, because member
    # 0 IS the cube; on an ensemble cube it would silently pin thermal derating and reservoir wetness to
    # member 0 while the demand/RES shapes follow `--draw`, i.e. evaluate the heat-wave coupling this model
    # exists to carry on a DIFFERENT summer from the one being priced. `project(realization=)` now carries
    # it, so the two follow the same draw by construction rather than by the cube happening to have one
    # member. Reported below so the run says which realization it used.
    t0 = time.monotonic()
    env = dict(os.environ)
    env["POWERSIM_WEATHER_CUBE"] = cube
    code = ("import sys; sys.path.insert(0, r'%s');"
            "from availability_model.config import load_config;"
            "from availability_model.projection.engine import project;"
            "project(load_config(r'%s'), n_draws=1, layer='availability_mc', "
            "partitions={'wdraw': %d}, draw_ids=[%d], realization=%d)"
            % (str(_ROOT / "availability_model"), str(_ROOT / "availability_model" / "config.yaml"),
               draw, draw, draw))
    r = subprocess.run([sys.executable, "-u", "-X", "utf8", "-W", "ignore", "-c", code], env=env)
    if r.returncode != 0:
        raise RuntimeError(f"step-v availability projection failed (exit code {r.returncode})")
    print(f"[20y] step-v availability projected on {Path(cube).name} realization={draw} "
          f"-> availability_mc/wdraw={draw} ({time.monotonic() - t0:.0f}s)", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--ref-year", type=int, default=2019)
    ap.add_argument("--draw", type=int, default=0)
    ap.add_argument("--out", default="scratchpad/proj20y")
    ap.add_argument("--years", default=None,
                    help="explicit comma-separated years instead of a contiguous range, e.g. "
                         "2030,2035,2040,2046. One preload serves them all, which is what makes a "
                         "sparse comparison affordable when the preload costs more than a solved year.")
    ap.add_argument("--n-weeks", type=int, default=None, help="truncate each year (smoke runs)")
    ap.add_argument("--no-avail-weather", action="store_true",
                    help="skip the step-v re-projection; use the canonical shared availability tables")
    ap.add_argument("--refresh-avail", action="store_true",
                    help="re-project step v even if availability_mc/wdraw=<draw> already exists "
                         "(needed after the ambient weather cube changed)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    hz = cfg.section("projection").get("horizon", {})
    start = args.start or int(hz.get("start_year", 2027))
    end = args.end or int(hz.get("end_year", 2046))
    years = ([int(y) for y in args.years.replace(" ", "").split(",") if y]
             if args.years else list(range(start, end + 1)))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # the capture is what makes this run different from the gate's; set before any solve
    os.environ["DISPATCH_CAPTURE_DISPATCH"] = "1"

    # Weather-coherent step-v availability. MUST precede `_preload`, which is where the projection
    # reads the step-v lake (`fr_availability.load_fr_availability`, env-sensitive at call time).
    # Under run_montecarlo.py DISPATCH_AVAIL_WDRAW is already set (and step v already ran on the
    # draw's own cube) — the guard makes this block a no-op there.
    if not args.no_avail_weather and "DISPATCH_AVAIL_WDRAW" not in os.environ:
        _ensure_step_v_availability(args.draw, refresh=args.refresh_avail)
        os.environ["DISPATCH_AVAIL_WDRAW"] = str(args.draw)

    todo = [y for y in years if not (out / f"prices_{y}.parquet").exists()]
    _span = args.years if args.years else f"{start}-{end}"
    print(f"[20y] horizon {_span} ({len(years)} years), {len(todo)} to run, draw={args.draw}",
          flush=True)
    if not todo:
        print("[20y] nothing to do", flush=True)
        return 0

    t0 = time.monotonic()
    ref = _preload(cfg, args.ref_year)
    print(f"[20y] preload {time.monotonic() - t0:.0f}s  zones={len(ref['zones'])}  "
          f"markup={'yes' if ref.get('markup') else 'NO'}", flush=True)

    provider = None
    if cfg.section("projection").get("weather_coherent", True):
        from dispatch_model.weather_shapes import default_weather_provider
        provider = default_weather_provider

    stats = []
    prog = Progress(len(todo), f"projection {_span}")
    prog.__enter__()
    for y in todo:
        t1 = time.monotonic()
        ws = None
        if provider is not None:
            try:
                ws = provider(cfg, y, draw=args.draw, ref_year=args.ref_year)
            except Exception as exc:                                           # noqa: BLE001
                print(f"[20y] {y}: weather-coherent engines unavailable ({type(exc).__name__}: {exc}); "
                      f"falling back to reshaped reference-year weather", flush=True)
                provider = None
        sink: dict = {}
        st = project_year(cfg, y, ref, n_weeks=args.n_weeks, weather_shapes=ws, draw=args.draw, sink=sink)
        stats.append(st)

        # ATOMIC, AND PRICES LAST. `todo` above skips a year whose `prices_<y>.parquet` exists, so that
        # file is the completion marker for the whole year — and it used to be written FIRST, before
        # dispatch and flows. An interruption between the two writes therefore marked the year done with
        # its VOLUMES missing, and the deliverables build needs those for revenues and congestion rents.
        # A plain `to_parquet` is not atomic either: killed mid-write it leaves a truncated file that
        # `exists()` happily returns True for, so the year is skipped forever with a corrupt marker.
        # This matters because the ensemble is designed to be interrupted — an overnight schedule stops
        # the runner hard, once a night, for as many nights as the ensemble takes.
        # Write each to a temp name, fsync-free `os.replace` (atomic within a volume on Windows and
        # POSIX alike), and land `prices` only once the other two are safely in place.
        def _atomic(df, name, **kw):
            tmp = out / f".{name}.tmp"
            df.to_parquet(tmp, **kw)
            os.replace(tmp, out / name)

        smc, spot = sink["smc"], sink["spot"]
        px = pd.concat({"smc": smc, "spot": spot}, axis=1)
        px.index.name = "timestamp_utc"
        if sink.get("dispatch") is not None:
            d = sink["dispatch"]
            d.columns = [f"{z}|{t}" for z, t in d.columns]                     # parquet needs flat columns
            d.index.name = "timestamp_utc"
            _atomic(d, f"dispatch_{y}.parquet")
        if sink.get("flows") is not None:
            _atomic(sink["flows"], f"flows_{y}.parquet", index=False)
        _atomic(px, f"prices_{y}.parquet")                                     # the marker, written LAST

        drop = sink.get("dropped") or []
        prog.update(note=f"{y} FR {spot['FR'].mean():.1f} EUR/MWh")
        print(f"[20y] {y} done in {time.monotonic() - t1:.0f}s  hours={len(spot)}  "
              f"FR mean {spot['FR'].mean():.1f} (smc {smc['FR'].mean():.1f})"
              + (f"  DROPPED {len(drop)} window(s): {drop}" if drop else ""), flush=True)

    prog.close()
    pd.concat(stats, ignore_index=True).to_csv(out / "stats.csv", index=False)
    print(f"[20y] complete in {(time.monotonic() - t0) / 60:.1f} min -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
