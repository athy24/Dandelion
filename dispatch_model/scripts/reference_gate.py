"""The baseline gate: the sensitivity set runs only on a reference whose RE-RUN lands on the ensemble's centre.

THE RULE (user decision 2026-09-17). The baseline `s00_baseline` is re-run on the REFERENCE scenario, and for EVERY
projected year the French annual mean spot of that re-run must lie within `TOLERANCE` = 1 EUR/MWh of the mean over
the ensemble draws the year map was selected on (the map meta's `draws`, 0-29). The selection itself only bounded
each chosen draw's OWN archived run (scripts/select_reference_weather.py); the re-run adds what the construction
cannot replay -- the neighbours' weather in the 72 h taper either side of 1 January, solver time budgets -- so the
archive figure is a promise and this gate is the measurement. A year missing from the re-run fails the gate, and so
does a year priced on other hours than the draw's own run (a window the solver dropped): its mean is then not the
same statistic, whatever it reads.

The tolerance is a constant of this module and is not a command-line option: when a year fails, the SCENARIO is
adjusted (scripts/adjust_reference_map.py), never the tolerance.

THE STAMP. `reference_gate.json` in the baseline's directory records the verdict with what it was measured on: the
baseline's variant fingerprint, the year map's meta sha256, the reference check it read (its sha256), the sha256 of
the checker that wrote that check and of this module, and every year's gap. `gate_passed` accepts a stamp only when
all of these are the current ones and the stamp is newer than the baseline's `.sens_complete`; run_sensitivities
starts no other variant without it. A check of another zone than FR, or one measured on another map, never passes.
A gap is compared unrounded, and a gap that is not a number fails.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import reference_scenario as rs

TOLERANCE = 1.0                      # EUR/MWh, per year, |re-run FR annual mean spot - ensemble mean|
ZONE = "FR"
STAT = "mean_spot"
STAMP = "reference_gate.json"
CHECK = "reference_check.json"
CHECKER = Path(__file__).resolve().parent / "check_reference_weather.py"


def _sha_or_absent(p: Path) -> str:
    return rs.file_sha256(p) if Path(p).exists() else "ABSENT"


def check_is_current(check: dict) -> bool:
    """The check was written by the checker on disk now (older checkers wrote no sha: not current)."""
    return check.get("checker_sha256") == _sha_or_absent(CHECKER)


def evaluate(check: dict, years: list[int]) -> dict:
    """The verdict on one reference check (check_reference_weather.py's JSON) for the horizon `years`."""
    per_year = ((check.get("fr") or {}).get(STAT) or {}).get("per_year") or {}
    gaps = {int(y): float(r["ref_minus_ens"]) for y, r in per_year.items()}
    missing = sorted(set(years) - set(gaps))
    failing = {y: g for y, g in sorted(gaps.items()) if y in years and not abs(g) <= TOLERANCE}   # NaN fails
    hours = {int(y): (int(r.get("ref_hours", -1)), int(r.get("own_hours", -1))) for y, r in per_year.items()}
    off_hours = {y: h for y, h in sorted(hours.items()) if y in years and (h[0] != h[1] or h[0] < 0)}
    return {"passed": not missing and not failing and not off_hours, "tolerance_eur_mwh": TOLERANCE, "zone": ZONE,
            "statistic": STAT, "years": sorted(years), "missing_years": missing,
            "failing_years": {str(y): round(g, 4) for y, g in failing.items()},
            "hours_mismatch_years": {str(y): {"ref_hours": h[0], "own_hours": h[1]} for y, h in off_hours.items()},
            "max_abs_gap": max((abs(gaps[y]) for y in years if y in gaps), default=float("nan")),
            "gaps": {str(y): g for y, g in sorted(gaps.items()) if y in years},
            "reproduction": {str(y): round(float(r["ref_minus_own"]), 4) for y, r in sorted(per_year.items())
                             if int(y) in years}}


def write(run_dir: Path, years: list[int], fp: str) -> dict:
    """Evaluate `run_dir`'s reference check and write the stamp (atomically). Returns the stamp."""
    run_dir = Path(run_dir)
    check_f = run_dir / CHECK
    check = json.loads(check_f.read_text(encoding="utf-8"))
    res = evaluate(check, years)
    stamp = {**res, "variant_fingerprint": fp, "map_meta_sha256": rs.file_sha256(rs.META_PATH),
             "check_sha256": rs.file_sha256(check_f), "check_map_meta_sha256": check.get("map_meta_sha256"),
             "checker_sha256": check.get("checker_sha256"), "gate_sha256": _sha_or_absent(Path(__file__)),
             "evaluated_utc": datetime.now(UTC).isoformat(timespec="seconds")}
    refused = []
    if check.get("map_meta_sha256") != stamp["map_meta_sha256"]:
        refused.append("the reference check was measured on another year map")
    if check.get("zone", ZONE) != ZONE:
        refused.append(f"the reference check describes {check.get('zone')}, not {ZONE}")
    if not check_is_current(check):
        refused.append("the reference check was written by another version of check_reference_weather.py")
    if refused:
        stamp["passed"] = False
        stamp["refused"] = "; ".join(refused)
    tmp = run_dir / (STAMP + ".tmp")
    tmp.write_text(json.dumps(stamp, indent=1), encoding="utf-8")
    os.replace(tmp, run_dir / STAMP)
    return stamp


def load(run_dir: Path, fp: str) -> dict | None:
    """The stamp in `run_dir` if it is CURRENT (fingerprint, map meta, check sha, newer than the marker), else None."""
    run_dir = Path(run_dir)
    f, marker, check_f = run_dir / STAMP, run_dir / ".sens_complete", run_dir / CHECK
    if not (f.exists() and marker.exists() and check_f.exists()):
        return None
    try:
        stamp = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    current = (stamp.get("variant_fingerprint") == fp
               and marker.read_text(encoding="utf-8").strip() == fp
               and stamp.get("map_meta_sha256") == rs.file_sha256(rs.META_PATH)
               and stamp.get("check_sha256") == rs.file_sha256(check_f)
               and stamp.get("checker_sha256") == _sha_or_absent(CHECKER)
               and stamp.get("gate_sha256") == _sha_or_absent(Path(__file__))
               and f.stat().st_mtime_ns >= marker.stat().st_mtime_ns)
    return stamp if current else None


def gate_passed(run_dir: Path, fp: str) -> bool:
    stamp = load(run_dir, fp)
    return bool(stamp and stamp.get("passed"))
