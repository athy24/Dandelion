"""Run the sensitivity set on REFERENCE_WEATHER_20Y — one 20-year projection per variant.

A sensitivity answers "how much does THIS assumption move the price", so everything except that assumption
must be identical between the variant and the baseline. Here that means one shared weather trajectory
(REFERENCE_WEATHER_20Y, the per-year most-typical draw of the 50-draw ensemble), one shared step-v
availability partition, one shared set of weather drivers — and, per variant, either its own workbook or
its own environment flag. Nothing else differs, so a difference in the output is the assumption and not
the draw.

WHAT IS SHARED, AND WHY IT IS WARMED FIRST. Three artefacts are weather-only and cost minutes each:
`demand/projection_features`, `res/proj_drivers` (both keyed by realization, both invalidated when the cube
changes) and the step-v availability partition. Computed inside the variants they would be computed by three
workers at once, racing on the same lake path, and recomputed by every variant that started before the
first one finished. So the runner computes them ONCE, alone, before any variant starts, and the variants
only read.

THE AVAILABILITY PARTITION IS ITS OWN. `availability_mc/wdraw=0..49` belongs to the 50-draw ensemble and
is archived; the sensitivities are not draw 0 of anything, and writing over that partition would silently
redefine an archived result. They use `wdraw=900`, set through `DISPATCH_AVAIL_WDRAW` before the runner
starts, which also makes `run_projection_20y` skip its own step-v block (it defers to an env var already
set — the same deference that makes it a no-op under `run_montecarlo`).

RESUMABILITY, at two levels, because this is designed to be killed every morning at 07:45:
  * a variant whose `.sens_complete` marker carries its own fingerprint (`variant_fingerprints`) is skipped
    whole; a variant directory stamped by another model is moved to `_stale/`, never resumed from
  * inside a variant, `run_projection_20y` skips any year whose `prices_<y>.parquet` exists, and that file
    is written LAST and atomically, so its presence means dispatch and flows landed too

    python -u -X utf8 -W ignore scripts/run_sensitivities.py [--workers 3] [--only s01,s02]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]                 # …/PriceModeling/dispatch_model
ROOT = REPO.parent
CUBE = ROOT / "archive" / "REFERENCE_WEATHER_20Y.nc"
MANIFEST = ROOT / "archive" / "sensitivities" / "manifest.json"
#: step-v partition for the sensitivity set. Deliberately outside 0-49 (the archived ensemble draws).
AVAIL_WDRAW = 900


def _base_env() -> dict:
    env = dict(os.environ)
    env["POWERSIM_WEATHER_CUBE"] = str(CUBE)
    env["DISPATCH_AVAIL_WDRAW"] = str(AVAIL_WDRAW)
    env["DISPATCH_CAPTURE_DISPATCH"] = "1"
    env.pop("POWERSIM_SCENARIOS", None)                    # each variant sets its own; never inherit one
    return env


def _file_sha(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def variant_fingerprints(variants: list[dict], start: int, end: int, n_weeks: int | None) -> dict[str, str]:
    """Variant id -> the digest its years and its `.sens_complete` are keyed on: the model on disk, plus what
    makes it this variant -- the reference cube's identity, its environment, its workbook's bytes, the horizon.
    The ONE definition: the runner writes markers with it and every reader (consolidation, charts) checks them
    against it, so a reader cannot count a variant the runner would recompute."""
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from model_fingerprint import digest

    from powersim_core.weather_cube import cube_identity

    ref_id = json.dumps(cube_identity(CUBE), sort_keys=True)
    out = {}
    for v in variants:
        wb = v.get("workbook")
        if wb:
            p = Path(wb)
            wb = _file_sha(p if p.is_absolute() else REPO / p)          # the variant's subprocess runs in REPO
        out[v["id"]] = digest({"reference_cube": ref_id, "avail_wdraw": AVAIL_WDRAW, "start": start, "end": end,
                               "n_weeks": n_weeks, "variant": v["id"],
                               "variant_env": json.dumps(v.get("env") or {}, sort_keys=True),
                               "variant_workbook": wb or "default"})
    return out


def complete_variants(out_root: Path, fps: dict[str, str]) -> dict[str, Path]:
    """Variant id -> directory, for the variants whose `.sens_complete` names their fingerprint in `fps`. A marker
    of another model, or one written before markers carried a fingerprint, does not count."""
    done = {}
    for vid, fp in fps.items():
        m = Path(out_root) / vid / ".sens_complete"
        if m.exists() and m.read_text(encoding="utf-8").strip() == fp:
            done[vid] = Path(out_root) / vid
    return done


def warm(env: dict, force_avail: bool = False) -> None:
    """Compute the three shared weather-only artefacts once, before any variant runs."""
    t0 = time.monotonic()
    from powersim_core import lake
    have = False
    if not force_avail:
        try:
            _t = lake.read_table("availability_mc", "availability_by_tech", wdraw=AVAIL_WDRAW)
            # REUSE ONLY WHAT IS USABLE — see the same gate in run_projection_20y.py. A partition written
            # before step v carried `installed_mw` reads perfectly and is unusable downstream.
            if "installed_mw" not in _t.columns or not _t["installed_mw"].notna().all():
                raise ValueError("partition predates installed_mw")
            have = True
        except (FileNotFoundError, KeyError, ValueError, OSError):
            have = False
    if not have:
        print(f"[sens] step-v availability -> availability_mc/wdraw={AVAIL_WDRAW} "
              f"on {CUBE.name} (realization 0)", flush=True)
        av_root, av_cfg = ROOT / "availability_model", ROOT / "availability_model" / "config.yaml"
        code = (f"import sys; sys.path.insert(0, r'{av_root}');"
                "from availability_model.config import load_config;"
                "from availability_model.projection.engine import project;"
                f"project(load_config(r'{av_cfg}'), n_draws=1, layer='availability_mc', "
                f"partitions=dict(wdraw={AVAIL_WDRAW}), draw_ids=[0], realization=0)")
        r = subprocess.run([sys.executable, "-u", "-X", "utf8", "-W", "ignore", "-c", code], env=env)
        if r.returncode != 0:
            raise SystemExit(f"step-v availability failed (exit {r.returncode})")
    else:
        print(f"[sens] step-v availability: reusing availability_mc/wdraw={AVAIL_WDRAW}", flush=True)

    print("[sens] weather drivers (demand projection_features + res proj_drivers, realization 0)",
          flush=True)
    dm, rm = ROOT / "demand_model", ROOT / "res_model"
    code = ("import sys;"
            f"sys.path.insert(0, r'{dm}'); sys.path.insert(0, r'{rm}');"
            "from demand_model.config import load_config as d;"
            "from demand_model.projection.weather import projection_features;"
            "from res_model.config import load_config as r;"
            "from res_model.projection.drivers import projection_drivers;"
            f"projection_features(d(r'{dm / 'config.yaml'}'), realization=0);"
            f"projection_drivers(r(r'{rm / 'config.yaml'}'), realization=0)")
    rr = subprocess.run([sys.executable, "-u", "-X", "utf8", "-W", "ignore", "-c", code], env=env)
    if rr.returncode != 0:
        raise SystemExit(f"driver warm-up failed (exit {rr.returncode})")
    print(f"[sens] warm-up complete in {(time.monotonic() - t0)/60:.1f} min", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default=str(REPO / "reports" / "sens"))
    ap.add_argument("--only", default=None, help="comma-separated variant id prefixes")
    ap.add_argument("--start", type=int, default=2027)
    ap.add_argument("--end", type=int, default=2046)
    ap.add_argument("--refresh-avail", action="store_true")
    ap.add_argument("--allow-stale-reference", action="store_true",
                    help="run on a reference cube older than the current weather model (normally refused)")
    ap.add_argument("--warm-only", action="store_true")
    ap.add_argument("--n-weeks", type=int, default=None,
                    help="truncate each year -- SMOKE RUNS ONLY, and never into `reports/sens`: a "
                         "truncated year leaves a `prices_<y>.parquet` the real run would then skip")
    a = ap.parse_args()

    if not CUBE.exists():
        raise SystemExit(f"reference weather absent: {CUBE} (run scripts/build_reference_weather.py)")
    if not MANIFEST.exists():
        raise SystemExit(f"manifest absent: {MANIFEST} (run scripts/make_sensitivities.py)")
    # THE REFERENCE WEATHER MUST BE THIS MODEL'S WEATHER. It is stitched from Monte-Carlo draw cubes, and on
    # 2026-09-14 the 100 m wind co-generation was refit after it was built: a reference older than the weather
    # model it stands for is another model's weather, and nothing downstream would notice.
    _wmodels = [ROOT / "weathergen" / "models" / f for f in ("wind100.json", "fitted.json")]
    _newest = max((m.stat().st_mtime for m in _wmodels if m.exists()), default=0.0)
    if CUBE.stat().st_mtime < _newest and not a.allow_stale_reference:
        raise SystemExit(f"{CUBE.name} predates the current weather model (weathergen/models): rebuild the reference "
                         f"weather from an ensemble of this model, or pass --allow-stale-reference")
    variants = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if a.only:
        pref = tuple(p.strip() for p in a.only.split(",") if p.strip())
        variants = [v for v in variants if v["id"].startswith(pref)]

    out_root = Path(a.out)
    # A truncated year still writes `prices_<y>.parquet`, and that file is the resume marker: the real run
    # would skip the year forever, holding four weeks of dispatch as if it were twelve months. Refuse,
    # rather than trust the help text -- this runs unattended, and the damage is silent.
    if a.n_weeks and out_root.resolve() == (REPO / "reports" / "sens").resolve():
        raise SystemExit("--n-weeks into reports/sens would poison the real run; use another --out")
    out_root.mkdir(parents=True, exist_ok=True)
    env = _base_env()
    warm(env, force_avail=a.refresh_avail)
    if a.warm_only:
        return 0

    # A VARIANT'S YEARS BELONG TO ONE MODEL. `run_projection_20y` resumes on any `prices_<y>.parquet` it finds, so
    # without a fingerprint a relaunch reuses whatever an earlier model left in reports/sens/<id>. Each variant is
    # keyed on the model digest plus what makes it a variant: the reference cube, its env, its workbook.
    fps = variant_fingerprints(variants, a.start, a.end, a.n_weeks)

    def _complete(v) -> bool:
        return v["id"] in complete_variants(out_root, {v["id"]: fps[v["id"]]})

    todo = [v for v in variants if not _complete(v)]
    print(f"[sens] {len(variants)} variants, {len(todo)} to run, {a.workers} worker(s)", flush=True)

    years = list(range(a.start, a.end + 1))

    class Slot:
        """One variant in flight: the 20-year dispatch, then the deliverables build on its parquets.

        Two stages rather than one process because `build_projection_deliverables.py` is what turns the
        raw year parquets into the tables anyone reads — revenue by tech, capture prices, congestion rent,
        negative hours — the same step every Monte-Carlo draw ran. A variant without it is not a result,
        it is an intermediate; and it costs minutes against the dispatch's hours, so it runs in the slot.
        """

        def __init__(self, v):
            self.v = v
            self.dir = out_root / v["id"]
            fp = fps[v["id"]]
            stamp_f = self.dir / ".model_fingerprint"
            was = stamp_f.read_text(encoding="utf-8").strip() if stamp_f.exists() else "unstamped"
            if self.dir.exists() and any(self.dir.iterdir()) and was != fp:
                # another model's years: moved aside, never deleted and never resumed from
                dest = out_root / "_stale" / f"{v['id']}-{was}-{time.strftime('%Y%m%dT%H%M%S')}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(self.dir, dest)
                print(f"[sens] {v['id']}: results of model {was}, not {fp} - moved to {dest}", flush=True)
            self.dir.mkdir(parents=True, exist_ok=True)
            stamp_f.write_text(fp, encoding="utf-8")
            self.env = dict(env)
            if v.get("workbook"):
                self.env["POWERSIM_SCENARIOS"] = v["workbook"]
            self.env.update({k: str(x) for k, x in (v.get("env") or {}).items()})
            self.t0 = time.monotonic()
            self.stage = 0
            self.proc = None
            self.fh = (self.dir / "run.log").open("a", encoding="utf-8")
            self.fh.write(f"\n===== {v['id']} {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            self.fh.write(f"workbook: {v.get('workbook')}\nenv: {v.get('env')}\n")
            self.fh.flush()

        def _cmd(self):
            if self.stage == 0:
                cmd = [sys.executable, "-u", "-X", "utf8", "-W", "ignore",
                       "scripts/run_projection_20y.py", "--start", str(a.start), "--end", str(a.end),
                       "--draw", "0", "--out", str(self.dir)]
                return cmd + (["--n-weeks", str(a.n_weeks)] if a.n_weeks else [])
            return [sys.executable, "-u", "-X", "utf8", "-W", "ignore",
                    "scripts/build_projection_deliverables.py",
                    "--src", str(self.dir), "--out", str(self.dir)]

        def start(self):
            self.proc = subprocess.Popen(self._cmd(), cwd=str(REPO), env=self.env,
                                         stdout=self.fh, stderr=subprocess.STDOUT)
            return self.proc.pid

    def _years_done(d):
        return sum((d / f"prices_{y}.parquet").exists() for y in years)

    running: list = []
    queue = list(todo)
    done = 0
    while queue or running:
        while queue and len(running) < a.workers:
            sl = Slot(queue.pop(0))
            pid = sl.start()
            running.append(sl)
            print(f"[sens] start {sl.v['id']:24s} pid {pid}  -> {sl.dir.name}/run.log", flush=True)

        time.sleep(20)
        for sl in list(running):
            if sl.proc.poll() is None:
                continue
            rc = sl.proc.returncode
            if sl.stage == 0:
                if rc == 0 and _years_done(sl.dir) == len(years):
                    sl.stage = 1
                    sl.start()                                       # deliverables, on those parquets
                    continue
                running.remove(sl)
                sl.fh.close()
                print(f"[sens] PART  {sl.v['id']:24s} dispatch exit {rc}  "
                      f"{_years_done(sl.dir)}/{len(years)} years  "
                      f"{(time.monotonic()-sl.t0)/60:.0f} min", flush=True)
                continue
            running.remove(sl)
            sl.fh.close()
            ok = rc == 0 and (sl.dir / "projection_20y_analysis.xlsx").exists()
            if ok:
                # LAST, and only once every year AND the deliverables are on disk: this marker is what a
                # resumed night skips on, so it must certify everything it stands for.
                (sl.dir / "meta.json").write_text(json.dumps(sl.v, indent=1), encoding="utf-8")
                (sl.dir / ".sens_complete").write_text(fps[sl.v["id"]], encoding="utf-8")
                done += 1
            print(f"[sens] {'DONE ' if ok else 'PART '}{sl.v['id']:24s} deliverables exit {rc}  "
                  f"{(time.monotonic()-sl.t0)/60:.0f} min", flush=True)

    total = sum(1 for v in variants if _complete(v))
    print(f"[sens] complete this session: {done}; total complete: {total}/{len(variants)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
