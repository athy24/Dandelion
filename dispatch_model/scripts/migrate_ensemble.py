"""Keep an ensemble's solved years reusable across a change that cannot touch a solved year, and rebuild the
deliverables such a change makes stale.

WHY. `run_montecarlo` keys its work directory on `model_fingerprint.digest`, so a cached year is reused if and
only if the model that produced it is the model on disk. That digest used to include
`build_projection_deliverables.py`, which never touches a solved year: it reads the per-year parquets and
formats them. So fixing a formatting bug in the builder -- as on 2026-09-14, when every hourly export was found
interleaved across the twenty years -- would have renamed the work directory at the next restart and re-solved
the whole ensemble (~6 days on this machine) to reproduce parquets that were already correct.

The builder is now OUT of the solve digest (`model_fingerprint.NON_SOLVE`) and stamps its own version into each
deliverables directory (`.deliverables_fingerprint`). This script bridges the one-time change of digest and keeps
deliverables current:

  migrate   At driver startup, with no solver alive. If the work directory for the CURRENT solve digest does not
            exist but an earlier one does whose recorded component map is identical on every solve component,
            rename it, restamp the draw markers, and rebuild stale deliverables. Exit 2 -- refuse -- when earlier
            work exists but none matches: starting would silently re-solve the ensemble, and that is a decision
            for a person, not for a scheduled task. Exit 3 if solver processes are alive.
  rebuild   Safe while the ensemble runs: for COMPLETED draws whose deliverables stamp is not the current
            builder's, run the builder into a temp dir, re-attach the draw's weather tab, verify the hourly
            export against the solved parquets, move the files in, and write the stamp LAST.

    python -X utf8 scripts/migrate_ensemble.py migrate --out reports/mc50 [--dry-run] [--no-rebuild]
    python -X utf8 scripts/migrate_ensemble.py rebuild --out reports/mc50 --work scratchpad/mc/mc50-<digest>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timezone
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
REPO = _HERE.parent                                                   # dispatch_model/
sys.path.insert(0, str(_HERE))

import model_fingerprint as mf  # noqa: E402

BUILDER = _HERE / "build_projection_deliverables.py"
STAMP = ".deliverables_fingerprint"
MARKER = ".draw_complete"
WEATHER_SHEET = "Weather (this draw)"
_DEFAULT_NON_SOLVE = ("dispatch_model/scripts/build_projection_deliverables.py",)


def non_solve() -> tuple[str, ...]:
    """Component keys that cannot change a solved year. Owned by model_fingerprint; defaulted here so this
    script reads recorded maps correctly whichever side of the change it runs on."""
    return tuple(getattr(mf, "NON_SOLVE", _DEFAULT_NON_SOLVE))


def digest_of(comp: dict) -> str:
    """The model_fingerprint.digest formula, applied to an arbitrary component map."""
    blob = "\n".join(f"{k}={v}" for k, v in sorted(comp.items())).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def builder_digest() -> str:
    """Must equal what build_projection_deliverables.py writes into STAMP."""
    return hashlib.sha256(BUILDER.read_bytes()).hexdigest()[:12]


def solvers_alive() -> list[int]:
    """Pids of solver-side python processes. A work directory must never be renamed under a running draw."""
    if os.name != "nt":
        return []
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | Where-Object { "
           "$_.CommandLine -match 'run_montecarlo|run_projection_20y|run_sensitivities' } | "
           "ForEach-Object { $_.ProcessId }")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, text=True, timeout=120)
    return [int(x) for x in r.stdout.split() if x.strip().isdigit() and int(x) != os.getpid()]


def _move_dir(src: Path, dst: Path, tries: int = 6) -> None:
    for i in range(tries):                                            # OneDrive holds directory handles briefly
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(5)


def _restamp(out_root: Path, old: str, new: str) -> int:
    n = 0
    for m in sorted(out_root.glob(f"draw_*/{MARKER}")):
        if m.read_text(encoding="utf-8").strip() == old:
            m.write_text(new, encoding="utf-8")
            n += 1
    return n


# ------------------------------------------------------------------------------------------------ migrate
def migrate(out_root: Path, master_seed: int, n_weeks, dry: bool = False, rebuild_after: bool = True) -> int:
    name = out_root.name
    mc = REPO / "scratchpad" / "mc"
    extra = {"master_seed": master_seed, "n_weeks": n_weeks}
    cur = mf.components(extra)
    now = digest_of(cur)
    target = mc / f"{name}-{now}"
    earlier = sorted((p for p in mc.glob(f"{name}-*")
                      if p.is_dir() and p != target and (p / "FINGERPRINT.json").exists()),
                     key=lambda p: (p / "FINGERPRINT.json").stat().st_mtime)
    print(f"solve digest now: {now}")

    if target.exists():
        # an interrupted migration can leave the directory renamed and the markers not yet restamped
        mig = target / "MIGRATED.json"
        if mig.exists():
            old = json.loads(mig.read_text(encoding="utf-8")).get("from")
            if old and not dry:
                k = _restamp(out_root, old, now)
                if k:
                    print(f"completed an interrupted migration: {k} marker(s) restamped {old} -> {now}")
        print(f"work dir {target.name} exists - nothing to migrate")
        if earlier:
            print(f"(earlier work dirs present, ignored: {[p.name for p in earlier]})")
        return _maybe_rebuild(out_root, target, now, dry, rebuild_after)

    if not earlier:
        print("no work dir for this model and none earlier - a fresh ensemble, nothing to migrate")
        return 0

    drop = set(non_solve())
    matches = []
    for p in earlier:
        rec = json.loads((p / "FINGERPRINT.json").read_text(encoding="utf-8"))["components"]
        if digest_of({k: v for k, v in rec.items() if k not in drop}) == now:
            matches.append((p, rec))

    if not matches:
        p = earlier[-1]
        rec = json.loads((p / "FINGERPRINT.json").read_text(encoding="utf-8"))["components"]
        changed = sorted(k for k in (set(rec) | set(cur)) - drop if rec.get(k) != cur.get(k))
        print(f"REFUSING: {len(earlier)} earlier work dir(s) exist but none was produced by the current solve "
              f"model; starting now would re-solve the ensemble from scratch.")
        print(f"  most recent: {p.name}; solve components that differ ({len(changed)}):")
        for k in changed[:40]:
            print(f"    {k}")
        return 2
    if len(matches) > 1:
        print(f"REFUSING: {len(matches)} earlier work dirs match this solve model "
              f"({[m[0].name for m in matches]}); merging them is a decision for a person")
        return 2

    src, rec = matches[0]
    old = src.name[len(name) + 1:]
    changed = sorted(k for k in set(rec) | set(cur) if rec.get(k) != cur.get(k))
    n_markers = sum(1 for m in out_root.glob(f"draw_*/{MARKER}")
                    if m.read_text(encoding="utf-8").strip() == old)
    print(f"{src.name} was produced by the same solve model; it differs only in non-solve components: {changed}")

    alive = solvers_alive()
    if dry:                                                           # a preview must work while the ensemble runs
        print(f"DRY RUN: would rename {src.name} -> {target.name} and restamp {n_markers} marker(s) {old} -> {now}"
              + (f"  [solvers alive {alive}: a real run refuses until they stop]" if alive else ""))
        return _maybe_rebuild(out_root, src, old, True, rebuild_after)
    if alive:
        print(f"REFUSING: solver process(es) alive {alive} - a work dir cannot be renamed under a running draw")
        return 3

    _move_dir(src, target)
    (target / "MIGRATED.json").write_text(json.dumps({
        "from": old, "to": now, "renamed_from": src.name,
        "non_solve_components_changed": changed,
        "why": "solve components byte-identical to the recorded map; only non-solve components differ",
        "utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }, indent=1), encoding="utf-8")
    k = _restamp(out_root, old, now)
    mf.stamp(target, extra)
    print(f"renamed {src.name} -> {target.name}; {k} marker(s) restamped {old} -> {now}")
    return _maybe_rebuild(out_root, target, now, dry, rebuild_after)


def _maybe_rebuild(out_root: Path, work: Path, fp: str, dry: bool, enabled: bool) -> int:
    if dry or not enabled:
        stale = _stale_draws(out_root, fp, None)
        print(f"{len(stale)} completed draw(s) with stale deliverables"
              + (" (not rebuilt: dry run)" if dry else " (not rebuilt: --no-rebuild)"))
        return 0
    return rebuild(out_root, work, fingerprint=fp)


# ------------------------------------------------------------------------------------------------ rebuild
def _stale_draws(out_root: Path, fingerprint: str | None, draws: set[int] | None) -> list[tuple[int, Path]]:
    bd = builder_digest()
    todo = []
    for ddir in sorted(out_root.glob("draw_*")):
        m = ddir / MARKER
        if not m.exists():
            continue                                                  # in flight: the running ensemble owns it
        if fingerprint is not None and m.read_text(encoding="utf-8").strip() != fingerprint:
            continue
        n = int(ddir.name.split("_")[1])
        if draws is not None and n not in draws:
            continue
        st = ddir / STAMP
        if st.exists() and st.read_text(encoding="utf-8").strip() == bd:
            continue
        todo.append((n, ddir))
    return todo


def attach_weather_tab(ddir: Path) -> None:
    """The tab run_montecarlo adds after the builder, rebuilt from weather_summary.csv (the cube is long gone)."""
    from openpyxl import load_workbook
    ws = pd.read_csv(ddir / "weather_summary.csv")
    xl = ddir / "projection_20y_analysis.xlsx"
    with pd.ExcelWriter(xl, engine="openpyxl", mode="a", if_sheet_exists="replace") as w:
        ws.round(2).to_excel(w, sheet_name=WEATHER_SHEET, index=False)
    wb = load_workbook(xl)
    if WEATHER_SHEET in wb.sheetnames:                                # just after the Notice, as run_montecarlo does
        wb.move_sheet(WEATHER_SHEET, offset=-(len(wb.sheetnames) - 2))
        wb.save(xl)


def verify_hourly(out: Path, src: Path) -> None:
    """The hourly export must be strictly increasing, carry every solved hour of every year, and agree with the
    per-year FR mean in price_summary (to the 2-decimal rounding of the export)."""
    h = pd.read_csv(out / "hourly_spot_prices.csv", parse_dates=["timestamp_utc"])
    ts = h["timestamp_utc"]
    if not (ts.is_monotonic_increasing and ts.is_unique):
        raise AssertionError("hourly export is not strictly increasing in time")
    ps = pd.read_csv(out / "price_summary.csv")
    years = sorted(int(f.stem.split("_")[1]) for f in src.glob("prices_*.parquet"))
    if sorted(ts.dt.year.unique()) != years:
        raise AssertionError(f"hourly export years {sorted(ts.dt.year.unique())} != solved years {years}")
    for y in years:
        n_solved = len(pd.read_parquet(src / f"prices_{y}.parquet"))
        sel = h[ts.dt.year == y]
        if len(sel) != n_solved:
            raise AssertionError(f"{y}: {len(sel)} hourly rows vs {n_solved} solved hours")
        ref = float(ps[(ps["year"] == y) & (ps["zone"] == "FR")]["mean_spot"].iloc[0])
        if abs(float(sel["FR"].mean()) - ref) > 0.006:
            raise AssertionError(f"{y}: hourly FR mean {sel['FR'].mean():.4f} != price_summary {ref:.4f}")


def _rebuild_one(n: int, ddir: Path, src: Path, bd: str) -> None:
    if not any(src.glob("prices_*.parquet")):
        raise FileNotFoundError(f"no per-year parquets in {src}")
    (ddir / STAMP).unlink(missing_ok=True)                            # stale from here until the very end
    tmp = ddir / f".rebuild_{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    r = subprocess.run([sys.executable, "-u", "-X", "utf8", "-W", "ignore", str(BUILDER),
                        "--src", str(src), "--out", str(tmp)], cwd=str(REPO), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"builder exit {r.returncode}: {r.stderr[-600:]}")
    (tmp / STAMP).unlink(missing_ok=True)                             # written into ddir LAST, below
    if (ddir / "weather_summary.csv").exists():
        shutil.copy2(ddir / "weather_summary.csv", tmp / "weather_summary.csv")
        attach_weather_tab(tmp)
    verify_hourly(tmp, src)
    for f in sorted(tmp.iterdir()):
        os.replace(f, ddir / f.name)
    tmp.rmdir()
    (ddir / STAMP).write_text(bd, encoding="utf-8")


def rebuild(out_root: Path, work: Path, fingerprint: str | None = None, draws: set[int] | None = None) -> int:
    bd = builder_digest()
    todo = _stale_draws(out_root, fingerprint, draws)
    print(f"builder {bd}: {len(todo)} completed draw(s) with stale deliverables", flush=True)
    failed = []
    for n, ddir in todo:
        t0 = time.monotonic()
        try:
            _rebuild_one(n, ddir, work / f"proj_{n:03d}", bd)
            print(f"  draw {n:3d}: rebuilt and verified in {time.monotonic() - t0:.0f}s", flush=True)
        except Exception as exc:                                      # noqa: BLE001 - one draw must not stop the rest
            failed.append(n)
            print(f"  draw {n:3d}: FAILED {type(exc).__name__}: {exc}", flush=True)
    if failed:
        print(f"{len(failed)} draw(s) failed: {failed} - their stamp stays absent, so the next pass retries them")
    return 1 if failed else 0


def _parse_draws(s: str | None) -> set[int] | None:
    if not s:
        return None
    out: set[int] = set()
    for part in s.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b) + 1) if b else [int(a)])
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate")
    m.add_argument("--out", required=True)
    m.add_argument("--master-seed", type=int, default=20260629)       # run_montecarlo's default
    m.add_argument("--n-weeks", type=int, default=None)
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--no-rebuild", action="store_true")
    b = sub.add_parser("rebuild")
    b.add_argument("--out", required=True)
    b.add_argument("--work", required=True)
    b.add_argument("--fingerprint", default=None, help="only draws whose marker carries this digest")
    b.add_argument("--draws", default=None, help="e.g. 0-20 or 3,7,11")
    a = ap.parse_args()
    rel = lambda p: Path(p) if Path(p).is_absolute() else REPO / p    # noqa: E731
    if a.cmd == "migrate":
        return migrate(rel(a.out), a.master_seed, a.n_weeks, dry=a.dry_run, rebuild_after=not a.no_rebuild)
    return rebuild(rel(a.out), rel(a.work), fingerprint=a.fingerprint, draws=_parse_draws(a.draws))


if __name__ == "__main__":
    raise SystemExit(main())
