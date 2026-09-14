"""Monte-Carlo projection: N independent weather draws, each carried coherently through steps iii-vii.

For every draw d this runs, in order:

  1. weathergen simulates a COMPLETE new 20-year hourly trajectory (42 stations, 8 variables), seeded by
     SeedSequence([master_seed, d]) so draw d is reproducible regardless of order or of which draws ran;
  2. `POWERSIM_WEATHER_CUBE` points every downstream model at that cube — demand (iii), RES (iv),
     availability (v) and the dispatch preload all read the same file, which is what makes the draw
     coherent rather than four unrelated randomisations;
  3. step v re-runs on that weather, so plant availability (thermal derating, reservoir wetness) follows
     the draw instead of being frozen;
  4. the 20-year dispatch runs;
  5. deliverables are written to `<out>/draw_<ddd>/`, with the draw's own weather summary as a tab.

**Parallel across draws.** Each draw is fully isolated: its own weather cube, its own step-v availability
(lake layer `availability_mc`, partition `wdraw=d`, selected by `DISPATCH_AVAIL_WDRAW`), its own demand/RES
driver cache (lake partition `realization=d`) and its own output directory. Nothing is shared and written,
so `--workers K` runs K draws concurrently. Pick K against RAM, not cores: each worker holds a full
projection preload plus its HiGHS model, and the machine this was built on fits about 3.

**Resumable, and only within one model.** A draw whose `.draw_complete` marker matches the CURRENT
model fingerprint is skipped, and inside a draw `run_projection_20y` reuses any year already computed under
that same fingerprint. That matters: one draw is ~3.3 h. What it must never do is reuse a year computed by a
different model, which has now happened twice -- see `scripts/model_fingerprint.py` and the comment in
`run_draw`. Both the work directory and the marker are keyed on a digest of the code, the configs, the
workbook and the seed, so stale work is invisible rather than silently trusted.

    python -u -X utf8 -W ignore scripts/run_montecarlo.py --draws 50 --workers 3 [--out reports/mc]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

_ROOT = _HERE.parents[1]


def _run(cmd: list[str], env: dict, label: str) -> None:
    t0 = time.monotonic()
    r = subprocess.run(cmd, env=env, cwd=str(_HERE.parent))
    if r.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {r.returncode}")
    print(f"    [{label}] {time.monotonic() - t0:.0f}s", flush=True)


def run_draw(draw: int, out_root: Path, master_seed: int, keep_cube: bool,
             n_weeks: int | None = None, *, fp: str) -> None:
    from mc_weather import generate_cube, weather_summary

    # THE MODEL MUST NOT CHANGE UNDER A RUNNING ENSEMBLE. `fp` was computed when the ensemble started, but every
    # draw imports whatever is on disk NOW, in fresh interpreters: an edit or a refit during a multi-day run would
    # put two models under one digest. Re-hashing costs ~0.7 s against ~3 h per draw.
    from model_fingerprint import digest as _digest
    _now = _digest({"master_seed": master_seed, "n_weeks": n_weeks})
    if _now != fp:
        raise RuntimeError(f"draw {draw}: the model changed under the running ensemble (launched as {fp}, files "
                           f"now hash to {_now}); nothing written - stop, and relaunch deliberately")

    ddir = out_root / f"draw_{draw:03d}"
    # A `.draw_complete` marker, written LAST, is what says a draw is finished — not the workbook.
    # `projection_20y_analysis.xlsx` is created by the deliverables step and only THEN gains its weather
    # tab, so an interruption in between left a workbook that existed, counted as complete, and was
    # missing the tab that lets a price path be attributed to a winter. The ensemble is built to be
    # interrupted (an overnight schedule stops it hard once a night), so the marker has to be the last
    # thing written, not the first artefact to appear.
    # The marker also carries the model that wrote it. Without that it is the same trap as the work
    # directory, one level up and worse: a marker left by another model makes the draw skip ENTIRELY,
    # so not even the fingerprinted work directory gets a chance to notice.
    marker = ddir / ".draw_complete"
    if marker.exists():
        was = marker.read_text(encoding="utf-8").strip()
        if was == fp:
            print(f"[mc] draw {draw}: already complete, skipping", flush=True)
            return
        print(f"[mc] draw {draw}: marker was written by model {was or '(unstamped)'}, this is {fp} "
              f"— RECOMPUTING", flush=True)
        # retire the old marker and deliverables stamp NOW, not at the end: until this draw finishes, anything
        # that counts markers (consolidation, archive, charts) must not see it as complete
        marker.unlink(missing_ok=True)
        (ddir / ".deliverables_fingerprint").unlink(missing_ok=True)
    if (ddir / "projection_20y_analysis.xlsx").exists():
        print(f"[mc] draw {draw}: workbook present but incomplete (interrupted) — redoing deliverables",
              flush=True)
    print(f"[mc] draw {draw}: starting", flush=True)
    t0 = time.monotonic()

    # THE CUBE IS KEYED ON THE MODEL TOO. It used to live at scratchpad/mc/cube_<d>.nc and be reused whenever the
    # file existed: when the 100 m wind co-generation was refit on 2026-09-14, the three cubes left by draws
    # interrupted on the 11th would have been reused under the NEW digest, carrying the old wind into draws 21-23.
    # It now lives in the fingerprinted work directory beside the draw's years, and is reused only when its own
    # attributes name this model, this seed and this draw. `generate_cube` writes it atomically.
    work_root = Path("scratchpad/mc") / f"{out_root.name}-{fp}"
    cube = work_root / f"cube_{draw:03d}.nc"
    if cube.exists():
        try:
            import xarray as xr
            with xr.open_dataset(cube) as _ds:
                _ok = (str(_ds.attrs.get("model_fp")) == fp
                       and int(_ds.attrs.get("mc_master_seed", -1)) == int(master_seed)
                       and int(_ds.attrs.get("mc_draw", -1)) == int(draw))
        except Exception:                                                  # noqa: BLE001 - unreadable = not ours
            _ok = False
        if not _ok:
            print(f"    [weathergen] {cube.name} does not belong to model {fp}, seed {master_seed}, draw {draw}"
                  f" - regenerating", flush=True)
            cube.unlink(missing_ok=True)
    if not cube.exists():
        tw = time.monotonic()
        generate_cube(draw, cube, master_seed, model_fp=fp)
        print(f"    [weathergen] {time.monotonic() - tw:.0f}s -> {cube}", flush=True)

    env = dict(os.environ)
    env["POWERSIM_WEATHER_CUBE"] = str((_HERE.parent / cube).resolve())
    env["DISPATCH_CAPTURE_DISPATCH"] = "1"
    env["DISPATCH_AVAIL_WDRAW"] = str(draw)      # this draw's availability, not the shared tables

    # step v on THIS draw's weather: one availability realization per weather realization
    _run([sys.executable, "-u", "-X", "utf8", "-W", "ignore", "-c",
          "import sys; sys.path.insert(0, r'%s');"
          "from availability_model.config import load_config;"
          "from availability_model.projection.engine import project;"
          "project(load_config(r'%s'), n_draws=1, layer='availability_mc', "
          "partitions={'wdraw': %d}, draw_ids=[%d])"
          % (str(_ROOT / "availability_model"), str(_ROOT / "availability_model" / "config.yaml"),
             draw, draw)],
         env, "step-v availability")

    # NAMESPACED BY THE MODEL, not by a name someone chose. `run_projection_20y` skips any year whose
    # parquet exists — that is what makes a night's work survive a reboot — but "the file exists" is not
    # "the file came from this model", and conflating the two has now cost two ensembles.
    #
    # The first fix keyed this directory on `out_root.name`, which only works if the operator remembers to
    # pick a new name. On 2026-09-09 the four-defect availability repair landed, `reports/mc50` and the
    # fifty availability partitions were deleted, and the ensemble was relaunched into `reports/mc50`.
    # `scratchpad/mc/mc50` survived, the name matched, and 41 of 50 draws reused PRE-REPAIR prices while
    # every marker and progress line reported 50/50. See `scripts/model_fingerprint.py`.
    #
    # `fp` is a digest of the model packages, the run scripts, the configs, the workbook and the seed. A
    # cached year is reused if and only if it was produced by the same model, and nothing has to be
    # remembered. It is computed in `main` and re-checked at the start of every draw (above).
    work = work_root / f"proj_{draw:03d}"
    # `--draw d` is what keys the demand/RES driver caches (lake partition `realization=d`). Passing 0 for
    # every draw would make them share ONE cache entry, and the mtime freshness test would then hand draw
    # d+1 the drivers computed for draw d whenever its cube predates that cache — a silent wrong answer.
    # The index no longer selects weather (the cube itself differs per draw), so it is free to use as a key.
    cmd = [sys.executable, "-u", "-X", "utf8", "-W", "ignore",
           "scripts/run_projection_20y.py", "--out", str(work), "--draw", str(draw)]
    if n_weeks:
        cmd += ["--n-weeks", str(n_weeks)]
    _run(cmd, env, "dispatch 20y")

    _run([sys.executable, "-u", "-X", "utf8", "-W", "ignore",
          "scripts/build_projection_deliverables.py", "--src", str(work), "--out", str(ddir)],
         env, "deliverables")

    # the draw's own weather, as a tab — without it a price path cannot be attributed to a winter
    import pandas as pd
    from openpyxl import load_workbook
    ws = weather_summary(_HERE.parent / cube)
    ws.to_csv(ddir / "weather_summary.csv", index=False)
    xl = ddir / "projection_20y_analysis.xlsx"
    with pd.ExcelWriter(xl, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        ws.round(2).to_excel(w, sheet_name="Weather (this draw)", index=False)
    wb = load_workbook(xl)
    if "Weather (this draw)" in wb.sheetnames:                      # place it just after the Notice
        wb.move_sheet("Weather (this draw)", offset=-(len(wb.sheetnames) - 2))
        wb.save(xl)

    marker.write_text(fp, encoding="utf-8")                         # LAST: see the guard at the top
    if not keep_cube:
        cube.unlink(missing_ok=True)                                # 472 MB each; 50 of them is 23 GB
    print(f"[mc] draw {draw}: done in {(time.monotonic() - t0) / 60:.1f} min -> {ddir}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=50, help="number of draws (0..N-1)")
    ap.add_argument("--start-draw", type=int, default=0)
    ap.add_argument("--out", default="reports/mc")
    ap.add_argument("--master-seed", type=int, default=20260629)
    ap.add_argument("--keep-cubes", action="store_true", help="retain each 472 MB cube after the draw")
    ap.add_argument("--workers", type=int, default=1,
                    help="draws in flight. Bound by RAM (a preload + HiGHS model each), not by cores")
    ap.add_argument("--n-weeks", type=int, default=None,
                    help="truncate each projected year — smoke-testing the chain, not a real ensemble")
    a = ap.parse_args()

    out_root = Path(a.out)
    out_root.mkdir(parents=True, exist_ok=True)
    todo = list(range(a.start_draw, a.draws))

    # The model's own digest, computed once. It names the work directory, so a cached year produced by a
    # different model is simply not found rather than silently trusted — see `run_draw`.
    from model_fingerprint import components, digest_of, stamp
    extra = {"master_seed": a.master_seed, "n_weeks": a.n_weeks}
    comp = components(extra)                          # ONE map: the digest and FINGERPRINT.json cannot disagree
    fp = digest_of(comp)
    work_root = Path("scratchpad/mc") / f"{out_root.name}-{fp}"
    stamp(work_root, extra, comp=comp)                # FINGERPRINT.json beside the draws it describes

    cached = sum(1 for d in todo if (work_root / f"proj_{d:03d}").is_dir())
    print(f"[mc] {len(todo)} draw(s) {todo[0]}..{todo[-1]}, master_seed={a.master_seed}, out={out_root}",
          flush=True)
    print(f"[mc] model fingerprint {fp} -> {work_root}  ({cached} draw(s) already cached for THIS model)",
          flush=True)
    others = sorted(p.name for p in Path("scratchpad/mc").glob(f"{out_root.name}-*")
                    if p.is_dir() and p.name != work_root.name)
    if others:
        print(f"[mc] {len(others)} work dir(s) from a DIFFERENT model present and correctly ignored: "
              f"{', '.join(others)}", flush=True)
        print("[mc]   diff them with: python scripts/model_fingerprint.py "
              "<a>/FINGERPRINT.json <b>/FINGERPRINT.json", flush=True)
    t0 = time.monotonic()
    from powersim_core.progress import Progress
    prog = Progress(len(todo), f"monte-carlo {len(todo)} draws")
    prog.__enter__()
    if a.workers <= 1:
        for d in todo:
            run_draw(d, out_root, a.master_seed, a.keep_cubes, a.n_weeks, fp=fp)
            prog.update(note=f"draw {d}")
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        done, failed = 0, []
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(run_draw, d, out_root, a.master_seed, a.keep_cubes, a.n_weeks, fp=fp): d
                    for d in todo}
            for f in as_completed(futs):
                d = futs[f]
                try:
                    f.result()
                except Exception as exc:                                   # noqa: BLE001
                    # one bad draw must not lose the ensemble: record it, keep the rest, report at the end
                    failed.append((d, f"{type(exc).__name__}: {exc}"))
                    print(f"[mc] draw {d} FAILED: {type(exc).__name__}: {exc}", flush=True)
                done += 1
                prog.update(note=f"draw {d}" + (" FAILED" if failed and failed[-1][0] == d else ""))
    prog.close()
    if a.workers > 1:
        if failed:
            print(f"[mc] {len(failed)} draw(s) failed: {failed}", flush=True)
            print("[mc] re-run the same command to retry them (completed draws are skipped)", flush=True)
    print(f"[mc] ensemble complete in {(time.monotonic() - t0) / 3600:.2f} h", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
