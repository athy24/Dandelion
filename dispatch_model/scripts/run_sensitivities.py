"""Run the sensitivity set on the REFERENCE scenario — one 20-year projection per variant.

A sensitivity answers "how much does THIS assumption move the price", so everything except that assumption
must be identical between the variant and the baseline. Here that means one shared REFERENCE scenario --
each year y taken whole from the Monte-Carlo draw d(y) that priced it at the ensemble's centre: its French
demand/RES frame, its step-v availability and its neighbours' weather (scripts/reference_scenario.py) -- and,
per variant, either its own workbook or its own environment flag. Nothing else differs, so a difference in the
output is the assumption and not the draw.

EACH VARIANT RUNS THROUGH `run_reference_20y.py`, which refuses a model other than the ensemble's, reference
artefacts built from another year map, and the projection's silent fallbacks (2019 weather, FR-proxy
neighbours, 2019 nuclear availability).

WHAT IS SHARED, AND WHY IT IS PREPARED FIRST. Three artefacts come from the year map and are only READ here:
the reference cube (`build_reference_weather.py`), the stitched availability `availability_mc/wdraw=901`
(`build_reference_availability.py`) and the driver snapshot the French frames are computed from. They are
verified before anything runs and never rebuilt by this runner -- a re-projection of step v, which the previous
version did whenever the partition looked unusable, would overwrite the stitch with one draw's outage calendar.
The French frames depend on a variant's `demand_*`/`res_*` tabs, so each distinct set is computed ONCE, alone,
before any variant starts (`run_reference_20y.py --frames-only`); the variants then only read the frame cache.

THE BASELINE RUNS FIRST, ALONE, AND GATES THE SET (user decision 2026-09-17). `s00_baseline` is dispatched before any
variant, its twenty years split across the workers (years are independent: nothing carries from one to the next),
then checked by `check_reference_weather.py` against draw d(y)'s own run and the ensemble mean, and judged by
`reference_gate.py`: for EVERY year, FR annual mean spot within 1 EUR/MWh of the mean of the map's ensemble draws,
priced on the same hours. Only then do the other variants start. When the gate fails the runner exits 4 and starts
nothing; run_all.ps1 then runs `adjust_reference_map.py`, rebuilds the reference and runs this again.

A WINDOW THE SOLVER DROPPED IS RE-RUN. A week dropped on a wall-clock budget moves a year's mean by up to a couple of
EUR/MWh and is not the scenario. After a dispatch, any year missing or priced on fewer hours than the ensemble's own
runs is set aside (`_short/`) and re-run, up to `REPAIR_PASSES` times, for the baseline and for every variant.

ONE LP ORDERING. PYTHONHASHSEED is pinned (0) for every run: the storage block is built from a set of zone names,
and an unpinned seed permutes the LP per process (run_reference_20y.py).

A FINISHED SET SAYS SO. When every variant of the manifest is complete, `.set_complete` (the fingerprints) is
written in the output root; run_all.ps1 then exits at start without sweeping or re-running anything, so an hourly
task left enabled neither burns the machine nor kills a manual run. Delete it to let the driver re-run the set.

EXIT CODES: 0 every variant complete; 3 some variant incomplete; 4 the baseline gate failed (adjust the map);
5 the baseline could not be completed or checked (not a scenario problem: a person looks).

THE RECORD. Everything the runner prints is also appended to `<out>/sens_history.log`, which no restart truncates
(the driver's `sens.out` is rewritten at each start). The real set runs on the map's whole horizon into
reports/sens; a partial `--start/--end` there is refused, as `--n-weeks` is.

RESUMABILITY, at two levels, because the driver restarts the runner after a reboot:
  * a variant whose `.sens_complete` marker carries its own fingerprint (`variant_fingerprints`) is skipped
    whole; a variant directory stamped by another model or reference is moved to `_stale/`, never resumed from
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
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]                 # …/PriceModeling/dispatch_model
ROOT = REPO.parent
_SCRIPTS = REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import reference_gate as gate  # noqa: E402
import reference_scenario as ref  # noqa: E402

CUBE = ref.CUBE_PATH
MANIFEST = ROOT / "archive" / "sensitivities" / "manifest.json"
#: step-v partition of the reference scenario (the stitched availability). Outside 0-49 (the ensemble draws).
AVAIL_WDRAW = ref.AVAIL_WDRAW
BASELINE = "s00_baseline"
SET_COMPLETE = ".set_complete"
#: re-runs of the years that came back missing or short (a dropped window), per variant
REPAIR_PASSES = 2
EXIT_INCOMPLETE, EXIT_GATE_FAILED, EXIT_BASELINE_FAILED = 3, 4, 5
#: the scripts whose behaviour a variant's years depend on beyond the model digest (they are not hashed in it)
_RUNNER_SCRIPTS = ("run_reference_20y.py", "reference_scenario.py")


def _base_env() -> dict:
    env = dict(os.environ)
    env["POWERSIM_WEATHER_CUBE"] = str(CUBE)
    env["DISPATCH_AVAIL_WDRAW"] = str(AVAIL_WDRAW)
    env["DISPATCH_CAPTURE_DISPATCH"] = "1"
    env["POWERSIM_RES_OFFSHORE_LOSSES"] = "1"              # the ensemble's offshore conversion (run_montecarlo)
    env["PYTHONHASHSEED"] = "0"                            # one LP ordering for every run (module docstring)
    env.pop("POWERSIM_SCENARIOS", None)                    # each variant sets its own; never inherit one
    return env


def _file_sha(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _sha_or_absent(path: Path) -> str:
    return _file_sha(path) if path.exists() else "ABSENT"


def variant_fingerprints(variants: list[dict], start: int, end: int, n_weeks: int | None) -> dict[str, str]:
    """Variant id -> the digest its years and its `.sens_complete` are keyed on: the model on disk, plus what
    makes it this variant -- the reference scenario (cube identity, year-map meta, stitched availability, driver
    snapshot, the reference runner), its environment, its workbook's bytes, the horizon.
    The ONE definition: the runner writes markers with it and every reader (consolidation, charts) checks them
    against it, so a reader cannot count a variant the runner would recompute."""
    from model_fingerprint import digest

    from powersim_core.weather_cube import cube_identity

    ref_id = json.dumps(cube_identity(CUBE), sort_keys=True)
    scenario = json.dumps({"map_meta": _sha_or_absent(ref.META_PATH),
                           "availability": ref.availability_digest(AVAIL_WDRAW),
                           "drivers": _sha_or_absent(ref.REF_DIR / "drivers" / "MANIFEST.json"),
                           "reference_draw": ref.REF_DRAW,
                           "runner": {s: _sha_or_absent(_SCRIPTS / s) for s in _RUNNER_SCRIPTS}}, sort_keys=True)
    out = {}
    for v in variants:
        wb = v.get("workbook")
        if wb:
            p = Path(wb)
            wb = _file_sha(p if p.is_absolute() else REPO / p)          # the variant's subprocess runs in REPO
        out[v["id"]] = digest({"reference_cube": ref_id, "reference_scenario": scenario, "avail_wdraw": AVAIL_WDRAW,
                               "start": start, "end": end, "n_weeks": n_weeks, "variant": v["id"],
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


def check_reference() -> None:
    """Refuse to start unless the reference artefacts are the ones the current year map describes."""
    sel, meta = ref.load_map()
    problems = ref.verify_cube(sel, meta) + ref.verify_availability(sel, meta, AVAIL_WDRAW)
    if problems:
        raise SystemExit("[sens] the reference scenario is not consistent with the year map: " + "; ".join(problems)
                         + " (run build_reference_weather.py / build_reference_availability.py)")
    print(f"[sens] reference scenario: {len(sel)} years from {len(set(sel.values()))} draws of ensemble "
          f"{meta['model_fingerprint']}; cube and availability_mc/wdraw={AVAIL_WDRAW} verified", flush=True)


def warm(env: dict, variants: list[dict]) -> None:
    """Compute the French frames of every distinct workbook, once and alone, before any variant runs."""
    t0 = time.monotonic()
    for v in variants:
        e = dict(env)
        if v.get("workbook"):
            e["POWERSIM_SCENARIOS"] = v["workbook"]
        print(f"[sens] FR frames for {v['id']}", flush=True)
        r = subprocess.run([sys.executable, "-u", "-X", "utf8", "-W", "ignore", "scripts/run_reference_20y.py",
                            "--frames-only"], cwd=str(REPO), env=e)
        if r.returncode != 0:
            raise SystemExit(f"[sens] frame computation failed for {v['id']} (exit {r.returncode})")
    print(f"[sens] frames ready for {len(variants)} variant(s) in {(time.monotonic() - t0)/60:.1f} min", flush=True)


def baseline_check_due(d: Path, fp: str) -> bool:
    """s00 is complete under `fp` and its reference check is missing, older than its marker, unreadable, or written
    by another version of check_reference_weather.py."""
    m, r = d / ".sens_complete", d / "reference_check.json"
    if not (m.exists() and m.read_text(encoding="utf-8").strip() == fp):
        return False
    if not r.exists() or r.stat().st_mtime_ns < m.stat().st_mtime_ns:
        return True
    try:
        return not gate.check_is_current(json.loads(r.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return True


class _Tee:
    """stdout, also appended to a log no restart truncates."""

    def __init__(self, stream, path: Path):
        self.stream, self.fh = stream, Path(path).open("a", encoding="utf-8")

    def write(self, text):
        self.stream.write(text)
        self.fh.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()
        self.fh.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def move_aside(src: Path, dest: Path, attempts: int = 6) -> None:
    """os.replace with retries: OneDrive holds directory handles briefly (as in migrate_ensemble._move_dir)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for i in range(attempts):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(5 * (i + 1))


def expected_hours(years: list[int], mc: Path | None = None) -> dict[int, int]:
    """FR priced hours per year in the ensemble's own runs (the most common count over the map meta's draws): 8736,
    and 8640 in 2046, whose cube ends on 26 December. A year below it lost a window."""
    _, meta = ref.load_map()
    counts: dict[int, Counter] = {y: Counter() for y in years}
    for d in meta["draws"]:
        f = Path(mc or ref.MC_OUT) / f"draw_{int(d):03d}" / "negative_zero_hours.csv"
        if not f.exists():
            continue
        with f.open(encoding="utf-8") as fh:
            head = fh.readline().strip().split(",")
            iy, iz, ih = head.index("year"), head.index("zone"), head.index("hours")
            for line in fh:
                c = line.strip().split(",")
                if len(c) > ih and c[iz] == "FR" and int(c[iy]) in counts:
                    counts[int(c[iy])][int(c[ih])] += 1
    return {y: max(c.most_common(1)[0][0], 0) if c else -1 for y, c in counts.items()}


def year_hours(d: Path, y: int) -> int | None:
    """Rows of `prices_<y>.parquet` (from its metadata), or None when the year is absent."""
    f = Path(d) / f"prices_{y}.parquet"
    if not f.exists():
        return None
    import pyarrow.parquet as pq
    return int(pq.ParquetFile(f).metadata.num_rows)


def bad_years(d: Path, years: list[int], expected: dict[int, int] | None) -> list[int]:
    """Years absent, or (when `expected` is given) priced on another number of hours than the ensemble's runs."""
    out = []
    for y in years:
        h = year_hours(d, y)
        if h is None or (expected is not None and expected.get(y, -1) > 0 and h != expected[y]):
            out.append(y)
    return out


def set_aside_short(d: Path, years: list[int]) -> list[int]:
    """Move the files of years that EXIST but are short to `_short/`, so a resumed dispatch solves them again."""
    moved = []
    stamp = time.strftime("%Y%m%dT%H%M%S")
    for y in years:
        files = [Path(d) / f"{k}_{y}.parquet" for k in ("prices", "dispatch", "flows")]
        if not files[0].exists():
            continue
        dest = Path(d) / "_short" / f"{y}-{stamp}"
        dest.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.exists():
                move_aside(f, dest / f.name)
        moved.append(y)
    return moved


def split_years(years: list[int], n: int) -> list[list[int]]:
    """Disjoint, interleaved subsets, so the slow late years spread across the processes."""
    n = max(1, min(n, len(years)))
    return [years[i::n] for i in range(n)]


def check_baseline(d: Path) -> None:
    """Fidelity of the baseline against the ensemble (module docstring). Reports; never raises."""
    log = d / "reference_check.log"
    with log.open("w", encoding="utf-8") as fh:
        r = subprocess.run([sys.executable, "-u", "-X", "utf8", "-W", "ignore", "scripts/check_reference_weather.py",
                            "--ref", str(d)], cwd=str(REPO), stdout=fh, stderr=subprocess.STDOUT)
    try:
        res = json.loads((d / "reference_check.json").read_text(encoding="utf-8"))
        m = res["fr"]["mean_spot"]
        print(f"[sens] REFERENCE CHECK {BASELINE}: FR mean spot |ref - own draw| mean {m['mean_abs_ref_minus_own']:.2f}"
              f" max {m['max_abs_ref_minus_own']:.2f}; |ref - ensemble| mean {m['mean_abs_ref_minus_ens']:.2f} max "
              f"{m['max_abs_ref_minus_ens']:.2f}, horizon {m['horizon_ref_minus_ens']:+.2f} EUR/MWh; years beyond "
              f"fr_tol {m['years_abs_ref_minus_ens_gt_fr_tol']} (see {log.name})", flush=True)
    except Exception as exc:                                            # noqa: BLE001 - a report must not stop the set
        print(f"[sens] REFERENCE CHECK {BASELINE}: FAILED to run (exit {r.returncode}, {type(exc).__name__}) - see "
              f"{log}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default=str(REPO / "reports" / "sens"))
    ap.add_argument("--only", default=None, help="comma-separated variant id prefixes")
    ap.add_argument("--start", type=int, default=2027)
    ap.add_argument("--end", type=int, default=2046)
    ap.add_argument("--warm-only", action="store_true")
    ap.add_argument("--n-weeks", type=int, default=None,
                    help="truncate each year -- SMOKE RUNS ONLY, and never into `reports/sens`: a "
                         "truncated year leaves a `prices_<y>.parquet` the real run would then skip")
    a = ap.parse_args()

    if not MANIFEST.exists():
        raise SystemExit(f"manifest absent: {MANIFEST} (run scripts/make_sensitivities.py)")
    check_reference()
    variants = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for v in variants:
        if v.get("workbook") and v.get("workbook_sha256") and ref.file_sha256(v["workbook"]) != v["workbook_sha256"]:
            raise SystemExit(f"[sens] {v['workbook']} changed after make_sensitivities built it")
    if a.only:
        pref = tuple(p.strip() for p in a.only.split(",") if p.strip())
        variants = [v for v in variants if v["id"].startswith(pref)]

    out_root = Path(a.out)
    # A truncated year still writes `prices_<y>.parquet`, and that file is the resume marker: the real run
    # would skip the year forever, holding four weeks of dispatch as if it were twelve months. Refuse,
    # rather than trust the help text -- this runs unattended, and the damage is silent.
    real_out = out_root.resolve() == (REPO / "reports" / "sens").resolve()
    if a.n_weeks and real_out:
        raise SystemExit("--n-weeks into reports/sens would poison the real run; use another --out")
    map_years = sorted(ref.load_map()[0])
    full_horizon = (a.start, a.end) == (map_years[0], map_years[-1])
    if real_out and not full_horizon:
        raise SystemExit(f"--start/--end {a.start}-{a.end} into reports/sens: the set runs on the map's whole horizon "
                         f"{map_years[0]}-{map_years[-1]}; use another --out for a partial run")
    out_root.mkdir(parents=True, exist_ok=True)
    sys.stdout = _Tee(sys.stdout, out_root / "sens_history.log")
    print(f"\n[sens] ===== run_sensitivities {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(sys.argv[1:])} =====",
          flush=True)
    env = _base_env()

    # A VARIANT'S YEARS BELONG TO ONE MODEL AND ONE REFERENCE. `run_projection_20y` resumes on any
    # `prices_<y>.parquet` it finds, so without a fingerprint a relaunch reuses whatever an earlier model left in
    # reports/sens/<id>. Each variant is keyed on the model digest plus the reference scenario, its env, its workbook.
    fps = variant_fingerprints(variants, a.start, a.end, a.n_weeks)

    def _complete(v) -> bool:
        return v["id"] in complete_variants(out_root, {v["id"]: fps[v["id"]]})

    todo = [v for v in variants if not _complete(v)]
    print(f"[sens] {len(variants)} variants, {len(todo)} to run, {a.workers} worker(s)", flush=True)
    whole_set = not a.only and not a.n_weeks and full_horizon
    if todo and whole_set:
        (out_root / SET_COMPLETE).unlink(missing_ok=True)
    years = list(range(a.start, a.end + 1))
    expected = None if a.n_weeks else expected_hours(years)
    full_manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def _finish(done_now: int) -> int:
        total = sum(1 for v in variants if _complete(v))
        print(f"[sens] complete this session: {done_now}; total complete: {total}/{len(variants)}", flush=True)
        if whole_set and total == len(variants):
            (out_root / SET_COMPLETE).write_text(json.dumps(fps, indent=1, sort_keys=True), encoding="utf-8")
            import build_reference_weather as brw
            n = brw.prune_cubes()
            print(f"[sens] the whole set is complete: {out_root / SET_COMPLETE} written; {n} regenerated cube(s) "
                  f"deleted", flush=True)
        return 0 if total == len(variants) else EXIT_INCOMPLETE

    def _prepare(v) -> tuple[Path, dict]:
        d = out_root / v["id"]
        fp = fps[v["id"]]
        stamp_f = d / ".model_fingerprint"
        was = stamp_f.read_text(encoding="utf-8").strip() if stamp_f.exists() else "unstamped"
        if d.exists() and any(d.iterdir()) and was != fp:
            # another model's or reference's years: moved aside, never deleted and never resumed from
            dest = out_root / "_stale" / f"{v['id']}-{was}-{time.strftime('%Y%m%dT%H%M%S')}"
            move_aside(d, dest)
            print(f"[sens] {v['id']}: results of model {was}, not {fp} - moved to {dest}", flush=True)
        d.mkdir(parents=True, exist_ok=True)
        stamp_f.write_text(fp, encoding="utf-8")
        e = dict(env)
        if v.get("workbook"):
            e["POWERSIM_SCENARIOS"] = v["workbook"]
        e.update({k: str(x) for k, x in (v.get("env") or {}).items()})
        return d, e

    def _dispatch_cmd(v, d, subset=None):
        cmd = [sys.executable, "-u", "-X", "utf8", "-W", "ignore", "scripts/run_reference_20y.py", "--out", str(d),
               "--allow-env", ",".join(sorted(v.get("env") or {}))]
        cmd += ["--years", ",".join(map(str, subset))] if subset else ["--start", str(a.start), "--end", str(a.end)]
        return cmd + (["--n-weeks", str(a.n_weeks)] if a.n_weeks else [])

    def _deliverables_cmd(d):
        return [sys.executable, "-u", "-X", "utf8", "-W", "ignore", "scripts/build_projection_deliverables.py",
                "--src", str(d), "--out", str(d)]

    def _mark_complete(v, d) -> None:
        # LAST, and only once every year AND the deliverables are on disk: this marker is what a resumed night
        # skips on, so it must certify everything it stands for.
        (d / "meta.json").write_text(json.dumps(v, indent=1), encoding="utf-8")
        (d / ".sens_complete").write_text(fps[v["id"]], encoding="utf-8")

    # ---- phase 1: the baseline, alone, then its gate ---------------------------------------------------------------
    # the gate holds for any selection of variants: `--only s05` still needs a baseline that passed
    base_v = next((x for x in full_manifest if x["id"] == BASELINE), None)
    gated = base_v is not None and not a.n_weeks
    if gated and BASELINE not in fps:
        fps.update(variant_fingerprints([base_v], a.start, a.end, a.n_weeks))
    if a.warm_only:                                              # frames only: nothing is dispatched
        warm(env, ([base_v] if gated else []) + [v for v in todo if v["id"] != BASELINE or not gated])
        return _finish(0)
    if gated:
        base_dir = out_root / BASELINE
        if not complete_variants(out_root, {BASELINE: fps[BASELINE]}):
            warm(env, [base_v])
            d, e = _prepare(base_v)
            with (d / "run.log").open("a", encoding="utf-8") as fh:
                fh.write(f"\n===== {BASELINE} (years split over {a.workers} processes) "
                         f"{time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                fh.flush()
                t0 = time.monotonic()
                for attempt in range(1 + REPAIR_PASSES):
                    todo_years = bad_years(d, years, expected)
                    if not todo_years:
                        break
                    moved = set_aside_short(d, todo_years)
                    if attempt or moved:
                        print(f"[sens] {BASELINE}: re-running years {todo_years} (set aside as short: {moved})",
                              flush=True)
                    procs = []
                    for i, subset in enumerate(split_years(todo_years, a.workers)):
                        if i:
                            time.sleep(30)                   # staggered: startup reads and stamps do not collide
                        procs.append(subprocess.Popen(_dispatch_cmd(base_v, d, subset), cwd=str(REPO), env=e,
                                                      stdout=fh, stderr=subprocess.STDOUT))
                        print(f"[sens] start {BASELINE} years {subset} pid {procs[-1].pid}", flush=True)
                    rcs = [p_.wait() for p_ in procs]
                    print(f"[sens] {BASELINE}: dispatch exits {rcs}, "
                          f"{len(years) - len(bad_years(d, years, expected))}/{len(years)} years complete "
                          f"({(time.monotonic() - t0) / 60:.0f} min)", flush=True)
                left = bad_years(d, years, expected)
                if left:
                    print(f"[sens] BASELINE FAILED: years {left} still missing or short after {REPAIR_PASSES} repair "
                          f"pass(es) - no variant is started", flush=True)
                    return EXIT_BASELINE_FAILED
                rc = subprocess.run(_deliverables_cmd(d), cwd=str(REPO), env=e, stdout=fh,
                                    stderr=subprocess.STDOUT).returncode
            if rc != 0 or not (d / "projection_20y_analysis.xlsx").exists():
                print(f"[sens] BASELINE FAILED: deliverables exit {rc} - no variant is started", flush=True)
                return EXIT_BASELINE_FAILED
            _mark_complete(base_v, d)
            print(f"[sens] DONE  {BASELINE} in {(time.monotonic() - t0) / 60:.0f} min", flush=True)
        if baseline_check_due(base_dir, fps[BASELINE]):
            check_baseline(base_dir)
        stamp = gate.load(base_dir, fps[BASELINE])
        if stamp is None and (base_dir / gate.CHECK).exists() and not baseline_check_due(base_dir, fps[BASELINE]):
            stamp = gate.write(base_dir, years, fps[BASELINE])
        if stamp is None:
            print(f"[sens] BASELINE GATE: no current reference check for {BASELINE} (see reference_check.log) - "
                  f"no variant is started", flush=True)
            return EXIT_BASELINE_FAILED
        worst = stamp.get("max_abs_gap")
        if not stamp.get("passed"):
            print(f"[sens] BASELINE GATE FAILED: FR annual mean off the ensemble mean by more than "
                  f"{gate.TOLERANCE} EUR/MWh in {sorted(stamp.get('failing_years', {}))} "
                  f"{stamp.get('failing_years')}; hours mismatch {stamp.get('hours_mismatch_years') or 'none'}; "
                  f"missing {stamp.get('missing_years') or 'none'} (worst |gap| {worst}) - no variant is started",
                  flush=True)
            return EXIT_GATE_FAILED
        print(f"[sens] BASELINE GATE PASSED: every year within {gate.TOLERANCE} EUR/MWh of the ensemble mean "
              f"(worst |gap| {worst} EUR/MWh)", flush=True)
    elif not a.n_weeks:
        raise SystemExit(f"[sens] {BASELINE} is not in the manifest: the set cannot be gated")

    todo = [v for v in todo if not (gated and v["id"] == BASELINE) and not _complete(v)]
    if not todo:
        return _finish(0)
    warm(env, todo)

    # ---- phase 2: the variants -------------------------------------------------------------------------------------
    class Slot:
        """One variant in flight: the 20-year dispatch (re-run for missing or short years), then the deliverables."""

        def __init__(self, v):
            self.v = v
            self.dir, self.env = _prepare(v)
            self.t0 = time.monotonic()
            self.stage = 0
            self.repairs = 0
            self.proc = None
            self.fh = (self.dir / "run.log").open("a", encoding="utf-8")
            self.fh.write(f"\n===== {v['id']} {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            self.fh.write(f"workbook: {v.get('workbook')}\nenv: {v.get('env')}\n")
            self.fh.flush()

        def start(self):
            cmd = _dispatch_cmd(self.v, self.dir) if self.stage == 0 else _deliverables_cmd(self.dir)
            self.proc = subprocess.Popen(cmd, cwd=str(REPO), env=self.env, stdout=self.fh, stderr=subprocess.STDOUT)
            return self.proc.pid

    def _advance(sl) -> str:
        """Move a slot whose process has exited to its next stage: 'running', 'done' or 'part'."""
        rc = sl.proc.returncode
        if sl.stage == 0:
            left = bad_years(sl.dir, years, expected)
            if not left:
                sl.stage = 1
                sl.start()                                           # deliverables, on those parquets
                return "running"
            if sl.repairs < REPAIR_PASSES:
                sl.repairs += 1
                moved = set_aside_short(sl.dir, left)
                print(f"[sens] {sl.v['id']}: dispatch exit {rc}, years {left} missing or short (set aside: "
                      f"{moved}) - repair pass {sl.repairs}", flush=True)
                sl.start()
                return "running"
            print(f"[sens] PART  {sl.v['id']:24s} dispatch exit {rc}  years {left} missing or short after "
                  f"{REPAIR_PASSES} repair pass(es)  {(time.monotonic()-sl.t0)/60:.0f} min", flush=True)
            return "part"
        ok = rc == 0 and (sl.dir / "projection_20y_analysis.xlsx").exists()
        if ok:
            _mark_complete(sl.v, sl.dir)
        print(f"[sens] {'DONE ' if ok else 'PART '}{sl.v['id']:24s} deliverables exit {rc}  "
              f"{(time.monotonic()-sl.t0)/60:.0f} min", flush=True)
        return "done" if ok else "part"

    running: list = []
    queue = list(todo)
    done = 0
    while queue or running:
        while queue and len(running) < a.workers:
            v = queue.pop(0)
            try:
                sl = Slot(v)
                pid = sl.start()
            except Exception as exc:                                    # noqa: BLE001 - one variant, not the set
                print(f"[sens] PART  {v['id']:24s} could not start ({type(exc).__name__}: {exc})", flush=True)
                continue
            running.append(sl)
            print(f"[sens] start {sl.v['id']:24s} pid {pid}  -> {sl.dir.name}/run.log", flush=True)

        time.sleep(20)
        for sl in list(running):
            if sl.proc.poll() is None:
                continue
            # ONE VARIANT, NOT THE SET: an exception here (a Popen refused under commit pressure, a OneDrive lock that
            # outlasts move_aside's retries) must not end the runner while its siblings' processes keep running
            try:
                state = _advance(sl)
            except Exception as exc:                                    # noqa: BLE001
                print(f"[sens] PART  {sl.v['id']:24s} failed between stages ({type(exc).__name__}: {exc})",
                      flush=True)
                state = "part"
            if state != "running":
                running.remove(sl)
                sl.fh.close()
                done += state == "done"

    return _finish(done)


if __name__ == "__main__":
    raise SystemExit(main())
