"""Adjust the REFERENCE year map for the years whose re-run failed the baseline gate (scripts/reference_gate.py).

WHEN. `run_sensitivities.py` re-runs `s00_baseline`, checks it (check_reference_weather.py) and gates it: every year's
French annual mean spot within 1 EUR/MWh of the ensemble mean, on the same hours as the draw's own run. When the gate
fails it exits 4 and starts no variant; the driver then runs this script, rebuilds the reference from the adjusted
map and re-runs the baseline. The tolerance is never touched -- the SCENARIO is changed, year by year, and only where
the measurement says so.

WHAT A FAILING YEAR TELLS US. Its measured gap splits in two: (own - ens), what draw d(y)'s own archived run was off
the ensemble mean, which the selection bounded; and e = (ref - own), what the re-run added -- the neighbours' 72 h
seam taper, solver paths. A replacement draw c is predicted to pass when |dev_c| + |e| <= 1: the worst case for the
unknown sign of c's own re-run error, with the one measured for year y as its only available scale. For each
failing year, among the candidates left:

  1. the ensemble draws the map was selected on (the meta's `draws`, 0-29) predicted to pass. Among those that also
     keep `COMFORT` in hand (|dev_c| + |e| <= 1 - 0.25) the one whose other zones sit closest to their ensemble
     means wins -- the selection's own second criterion; when none keeps that margin, the one with the most FR
     headroom, so an adjustment does not buy a neighbour score with the margin that made the year fail;
  2. ONLY WITH `--allow-other-draws`: the same among the ensemble's other completed draws (30-49). The user asked
     for a reference "from the existing 30 draws" and has not approved widening the pool, so the driver does not
     pass it; the gate's centre would stay the mean of the meta's draws;
  3. otherwise the draw predicted CLOSEST once the sign of e is taken into account (|dev_c + e|), and only if that
     prediction improves on the year's measured gap -- a pick predicted to fail by more would only use up a round.

A candidate is never a draw already used for that year, one whose own run of that year was priced on fewer hours
(a dropped window), or one whose artefacts the chain could not build: its weather summary, the recorded size of its
cube, its demand/RES driver caches (snapshotted, or in the lake with a sidecar naming its cube and older than its
completion marker) and its four step-v availability partitions (older than the marker). Replacing d(y) also changes
the seam taper of y-1 and y+1, so the whole baseline is re-run and re-gated: an adjustment is a proposal the next
measurement accepts or not.

HOURS. A year whose re-run is priced on the ensemble's full hours but whose draw's own run is short is a scenario
problem -- the draw's archived mean is over fewer hours -- and that draw is replaced like a failing one. A re-run
that is itself short, or a year missing from it, is a solver problem: the runner's repair passes did not fix it, and
this script refuses.

WHEN IT REFUSES (exit 2): a stale gate stamp, a solver problem (above), a year with no candidate left, or
`MAX_ROUNDS` adjustments already made. The driver then stops without launching the set.

OUTPUT. The previous map and meta are copied to archive/reference_20y/history/; the new pair is written to temp names
and renamed META FIRST, each rename retried on the sharing violations OneDrive causes, and the old meta is put back if
the map cannot follow, so the two never disagree. The meta gains `adjustments` (round, year, draw before and after,
tier, measured gap and re-run error, the candidate's archive deviation and predictions, the check's sha256) and
`includes_draws_outside_selection`. Exit 0 = adjusted, 3 = the gate had passed (nothing to do), 1 = the write failed
(the previous pair is in place).

    python -X utf8 -W ignore scripts/adjust_reference_map.py [--run reports/sens/s00_baseline] [--allow-other-draws]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import reference_gate as gate  # noqa: E402
import reference_scenario as rs  # noqa: E402

MAX_ROUNDS = 3
REF_ZONE = "FR"
#: EUR/MWh kept in hand below the tolerance before the neighbours' score may decide between eligible draws
COMFORT = 0.25


def load_pool(mc: Path, fp: str, draws: list[int]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(price_summary, negative_zero_hours) of those `draws` complete under `fp`, long, with a `draw` column."""
    ps, nz = [], []
    for d in draws:
        dd = Path(mc) / f"draw_{d:03d}"
        if not rs.draw_complete(d, fp, mc) or not (dd / "price_summary.csv").exists():
            continue
        ps.append(pd.read_csv(dd / "price_summary.csv").assign(draw=d))
        if (dd / "negative_zero_hours.csv").exists():
            nz.append(pd.read_csv(dd / "negative_zero_hours.csv").assign(draw=d))
    return (pd.concat(ps, ignore_index=True) if ps else pd.DataFrame(),
            pd.concat(nz, ignore_index=True) if nz else pd.DataFrame())


def modal_hours(H: pd.DataFrame, ens: list[int], y: int) -> int:
    """The FR priced hours of year y in most of the ensemble's own runs (-1 when unknown)."""
    if H.empty:
        return -1
    h = H[(H["year"] == y) & (H["zone"] == REF_ZONE) & H["draw"].isin(ens)]["hours"]
    return int(h.mode().max()) if not h.empty else -1


def candidate_table(E: pd.DataFrame, H: pd.DataFrame, ens: list[int], y: int) -> pd.DataFrame:
    """Every pool draw's year-y statistics against the ENSEMBLE's (the meta's draws) mean and sd."""
    ey = E[E["year"] == y]
    P = ey.pivot(index="draw", columns="zone", values="mean_spot")
    mu, sd = P.loc[P.index.isin(ens)].mean(), P.loc[P.index.isin(ens)].std()
    others = [z for z in P.columns if z != REF_ZONE]
    Z = (P[others] - mu[others]) / sd[others]
    fr_h = H[(H["year"] == y) & (H["zone"] == REF_ZONE)].set_index("draw")["hours"] if not H.empty else pd.Series()
    full = modal_hours(H, ens, y)
    return pd.DataFrame({
        "dev": P[REF_ZONE] - mu[REF_ZONE], "oscore": (Z ** 2).mean(axis=1),
        "others_mean_abs_z": Z.abs().mean(axis=1), "others_max_abs_z": Z.abs().max(axis=1),
        "fr_draw_mean": P[REF_ZONE], "in_ensemble": P.index.isin(ens),
        "full_hours": [bool(full > 0 and int(fr_h.get(d, -1)) == full) for d in P.index]},
        index=P.index).assign(fr_ensemble_mean=float(mu[REF_ZONE]), fr_ensemble_sd=float(sd[REF_ZONE]))


def build_problems(d: int, fp: str, mc: Path) -> list[str]:
    """What the chain would refuse for draw d (empty = its reference artefacts can be built)."""
    import build_reference_weather as brw
    import run_reference_20y as rr

    from powersim_core import lake
    mc = Path(mc)
    marker = mc / f"draw_{d:03d}" / ".draw_complete"
    if not rs.draw_complete(d, fp, mc):
        return ["not complete under the ensemble"]
    out = []
    if not (mc / f"draw_{d:03d}" / "weather_summary.csv").exists():
        out.append("no weather_summary.csv")
    if brw.recorded_cube_size(d, fp) is None:
        out.append("no recorded cube size")
    man_f = rr.DRIVER_SNAPSHOT / "MANIFEST.json"
    files = json.loads(man_f.read_text(encoding="utf-8")).get("files", {}) if man_f.exists() else {}
    for layer, dataset in rr.DRIVER_TABLES:
        if f"{layer}/{dataset}/realization={d}" in files and rr._snap_part(layer, dataset, d).exists():
            continue
        part = lake.table_path(layer, dataset, realization=d)
        side = part.with_name("source_cube.json")
        if not (part.exists() and rr._sidecar_names_draw(side, fp, d, mc)
                and part.stat().st_mtime_ns <= marker.stat().st_mtime_ns):
            out.append(f"{layer}/{dataset} driver cache unusable")
    for t in rs.AVAIL_TABLES:
        part = lake.table_path("availability_mc", t, wdraw=d)
        if not (part.exists() and part.stat().st_mtime_ns <= marker.stat().st_mtime_ns):
            out.append(f"availability {t} unusable")
    return out


def choose(table: pd.DataFrame, e_y: float, gap_y: float, tried: set[int], allow_other: bool = False,
           buildable=None, tol: float = gate.TOLERANCE, comfort: float = COMFORT) -> tuple[int, str] | None:
    """(draw, tier) for one failing year, or None when no candidate is left (module docstring). `buildable(d)`
    returns True when the chain can build draw d's artefacts."""
    t = table[~table.index.isin(tried) & table["full_hours"]].copy()
    if not allow_other:
        t = t[t["in_ensemble"]]
    if buildable is not None and not t.empty:
        t = t[[bool(buildable(int(d))) for d in t.index]]
    if t.empty:
        return None
    t["worst"] = t["dev"].abs() + abs(e_y)
    ok = t[t["worst"] <= tol]
    for tier, sub in (("ensemble", ok[ok["in_ensemble"]]), ("other-draws", ok[~ok["in_ensemble"]])):
        if sub.empty:
            continue
        comfy = sub[sub["worst"] <= tol - comfort]
        if not comfy.empty:
            return int(comfy["oscore"].idxmin()), tier
        return int(sub["worst"].idxmin()), tier
    t["signed"] = (t["dev"] + e_y).abs()
    t = t.sort_values(["signed", "worst"])
    if t["signed"].iloc[0] >= abs(gap_y):
        return None                                          # nothing predicted to improve on the measured gap
    return int(t.index[0]), "fr-closest-signed"


def _replace(tmp: Path, dest: Path, attempts: int = 6) -> None:
    for i in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(3 * (i + 1))


def write_pair(new_map: dict, new_meta: dict, keep_meta: Path) -> None:
    """Meta first, then map; if the map cannot follow, the previous meta (`keep_meta`) is put back."""
    tm, tp = rs.META_PATH.with_name(rs.META_PATH.name + ".tmp"), rs.MAP_PATH.with_name(rs.MAP_PATH.name + ".tmp")
    tm.write_text(json.dumps(new_meta, indent=1), encoding="utf-8")
    tp.write_text(json.dumps(new_map), encoding="utf-8")
    _replace(tm, rs.META_PATH)
    try:
        _replace(tp, rs.MAP_PATH)
    except OSError:
        back = rs.META_PATH.with_name(rs.META_PATH.name + ".restore")
        shutil.copy2(keep_meta, back)
        _replace(back, rs.META_PATH)
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=str(rs.REPO / "reports" / "sens" / "s00_baseline"))
    ap.add_argument("--mc", default=str(rs.MC_OUT))
    ap.add_argument("--allow-other-draws", action="store_true",
                    help="let a year be taken from the ensemble's draws outside the map meta's `draws` (not approved "
                         "by default: the reference is to be built from the draws the map was selected on)")
    a = ap.parse_args()
    run = Path(a.run)

    sel, meta = rs.load_map()
    fp, ens = meta["model_fingerprint"], [int(d) for d in meta["draws"]]
    stamp_f, check_f = run / gate.STAMP, run / gate.CHECK
    if not stamp_f.exists() or not check_f.exists():
        print(f"[adjust] no gate stamp / check in {run}: nothing to act on")
        return 2
    stamp = json.loads(stamp_f.read_text(encoding="utf-8"))
    if stamp.get("map_meta_sha256") != rs.file_sha256(rs.META_PATH) or stamp.get("check_sha256") != rs.file_sha256(
            check_f):
        print("[adjust] the gate stamp is not the current map's or check's: refusing to act on it")
        return 2
    if stamp.get("passed"):
        print("[adjust] the baseline gate passed: nothing to adjust")
        return 3
    if stamp.get("refused"):
        print(f"[adjust] the gate refused its check ({stamp['refused']}): not a scenario problem - not adjusting")
        return 2
    if stamp.get("missing_years"):
        print(f"[adjust] years {stamp['missing_years']} are missing from the re-run: a solver problem - not adjusting")
        return 2
    history = list(meta.get("adjustments", []))
    rnd = len({h["round"] for h in history}) + 1
    if rnd > MAX_ROUNDS:
        print(f"[adjust] {MAX_ROUNDS} adjustment rounds already made: stopping (the set is NOT launched)")
        return 2

    check = json.loads(check_f.read_text(encoding="utf-8"))
    per_year = check["fr"]["mean_spot"]["per_year"]
    pool = sorted(set(ens) | ({int(p.name.split("_")[1]) for p in Path(a.mc).glob("draw_*")
                               if rs.draw_complete(int(p.name.split("_")[1]), fp, a.mc)} if a.allow_other_draws
                              else set()))
    E, H = load_pool(Path(a.mc), fp, pool)

    failing = {int(y): "gap" for y in stamp.get("failing_years", {})}
    for y, h in (stamp.get("hours_mismatch_years") or {}).items():
        full = modal_hours(H, ens, int(y))
        if full > 0 and int(h["ref_hours"]) == full:
            failing[int(y)] = "own run short"                  # the draw's archive is over fewer hours: replace it
        else:
            print(f"[adjust] {y}: the re-run itself is priced on {h['ref_hours']} h against {full}: a solver "
                  f"problem the repair passes did not fix - not adjusting")
            return 2
    e_scale = [abs(float(r["ref_minus_own"])) for y, r in per_year.items() if int(y) not in failing]
    e_typ = sum(e_scale) / len(e_scale) if e_scale else 0.0

    cache: dict[int, bool] = {}

    def buildable(d: int) -> bool:
        if d not in cache:
            problems = build_problems(d, fp, Path(a.mc))
            cache[d] = not problems
            if problems:
                print(f"[adjust]   draw {d} cannot be built ({'; '.join(problems)}): not a candidate")
        return cache[d]

    new_sel, records, per_year_meta = dict(sel), [], dict(meta.get("per_year", {}))
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for y, why in sorted(failing.items()):
        r = per_year[str(y)]
        e_y = float(r["ref_minus_own"]) if why == "gap" else e_typ
        gap_y = float(r["ref_minus_ens"]) if why == "gap" else float("inf")
        tried = {sel[y]} | {int(h["from_draw"]) for h in history if int(h["year"]) == y} | {
            int(h["to_draw"]) for h in history if int(h["year"]) == y}
        table = candidate_table(E, H, ens, y)
        pick = choose(table, e_y, gap_y, tried, a.allow_other_draws, buildable)
        if pick is None:
            print(f"[adjust] {y}: no candidate draw predicted to improve on it - not adjusting")
            return 2
        c, tier = pick
        row = table.loc[c]
        new_sel[y] = c
        n_ok = int(((table["dev"].abs() + abs(e_y)) <= gate.TOLERANCE)[table["in_ensemble"]].sum())
        records.append({"round": rnd, "year": y, "reason": why, "from_draw": sel[y], "to_draw": c, "tier": tier,
                        "measured": {"ref": r["ref"], "own": r["own"], "ens_mean": r["ens_mean"],
                                     "gap": r["ref_minus_ens"], "reproduction_error": float(r["ref_minus_own"])},
                        "candidate": {"dev": float(row["dev"]),
                                      "predicted_worst_gap": abs(float(row["dev"])) + abs(e_y),
                                      "predicted_signed_gap": abs(float(row["dev"]) + e_y),
                                      "others_mean_sq_z": float(row["oscore"]),
                                      "in_ensemble": bool(row["in_ensemble"])},
                        "gate_check_sha256": stamp["check_sha256"], "adjusted_utc": now})
        per_year_meta[str(y)] = {"n_draws": len(ens), "n_candidates": n_ok,
                                 "fr_ensemble_mean": round(float(row["fr_ensemble_mean"]), 4),
                                 "fr_ensemble_sd": round(float(row["fr_ensemble_sd"]), 4),
                                 "fr_draw_mean": round(float(row["fr_draw_mean"]), 4),
                                 "fr_dev": round(float(row["dev"]), 4),
                                 "others_mean_abs_z": round(float(row["others_mean_abs_z"]), 4),
                                 "others_max_abs_z": round(float(row["others_max_abs_z"]), 4),
                                 "adjusted_round": rnd}
        print(f"[adjust] {y} ({why}): gap {float(r['ref_minus_ens']):+.3f} (own {float(r['own_minus_ens']):+.3f}, "
              f"re-run {float(r['ref_minus_own']):+.3f}) -> draw {sel[y]} replaced by {c} [{tier}], archive dev "
              f"{float(row['dev']):+.3f}, predicted worst {abs(float(row['dev'])) + abs(e_y):.3f}")

    hist = rs.REF_DIR / "history"
    hist.mkdir(parents=True, exist_ok=True)
    keep_map, keep_meta = hist / f"round{rnd - 1}_{rs.MAP_PATH.name}", hist / f"round{rnd - 1}_{rs.META_PATH.name}"
    shutil.copy2(rs.MAP_PATH, keep_map)
    shutil.copy2(rs.META_PATH, keep_meta)
    all_adj = history + records
    new_meta = {**meta, "map": {str(y): d for y, d in sorted(new_sel.items())}, "per_year": per_year_meta,
                "adjustments": all_adj,
                "includes_draws_outside_selection": any(h.get("tier") == "other-draws" for h in all_adj),
                "adjustment_rule": ("per failing year of the baseline gate: draws of the meta's `draws` predicted to "
                                    "pass (|archive dev| + |measured re-run error| <= 1 EUR/MWh), neighbours closest "
                                    f"among those keeping {COMFORT} EUR/MWh in hand, else the most FR headroom; the "
                                    "ensemble's other draws only with --allow-other-draws; else the draw predicted "
                                    "closest with the error's sign, only if it improves on the measured gap; never a "
                                    "draw already tried, short on hours, or whose artefacts cannot be built")}
    try:
        write_pair(new_meta["map"], new_meta, keep_meta)
    except OSError as exc:
        print(f"[adjust] could not write the adjusted map ({exc}); the previous map and meta are in place")
        return 1
    print(f"[adjust] round {rnd}: {len(records)} year(s) adjusted; map now uses "
          f"{len(set(new_sel.values()))} draws: {sorted(set(new_sel.values()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
