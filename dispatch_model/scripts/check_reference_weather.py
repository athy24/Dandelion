"""Does a run on the REFERENCE scenario give the prices it was built to give? Two questions, kept apart.

REPRODUCTION -- year y of the reference run against draw d(y)'s OWN run. The construction replays d(y)'s French
demand/RES frame, its step-v availability and its model exactly (scripts/reference_scenario.py), so what is left is
what it cannot replay: the neighbours' weather in the 72 hours either side of each 1 January (the seam taper of
build_reference_weather.py) and the solver's wall-clock budgets (a window dropped in one run and not the other).
This is the measure of the construction itself.

HOURS -- a window the solver dropped (wall-clock budget) removes a week from the year and can move its annual mean by
-2.4 to +1.5 EUR/MWh; the year's priced hours are therefore compared too (`ref_hours` against `own_hours`).

CENTRE -- the reference run against the mean of the ensemble draws the map was selected on (the map meta's
`draws`). A year adjusted by scripts/adjust_reference_map.py may take a draw outside that set; its own run is read
from its deliverables, and the centre stays the mean of the map meta's draws. This is what a sensitivity reads as
"the ensemble average". The selection held FR within `fr_tol` EUR/MWh
of that mean for d(y)'s own run; the re-run inherits that plus its reproduction error. Reported in EUR/MWh and as a
z-score against the between-draw standard deviation, for FR on every statistic and for every zone on the mean. The
FR annual-mean gap per year is what the baseline gate judges (scripts/reference_gate.py: within 1 EUR/MWh, every year).

    python -X utf8 -W ignore scripts/check_reference_weather.py --ref reports/sens/s00_baseline [--json PATH]

Reads the run's `price_summary.csv` / `negative_zero_hours.csv` (build_projection_deliverables writes them) or, when
they are absent, computes the same statistics from `prices_<y>.parquet` with the deliverables' definitions, so a
partial run can be checked on the years it has. The run must carry the `reference_run.json` stamp that
run_reference_20y.py writes, naming this map; the result is also written as JSON beside it, at full precision, with
the zone it describes and the sha256 of this script (a verdict of an older checker is not a current verdict). The
gate reads only the FR check at the default path: a check of another zone goes to `reference_check_<zone>.json`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import reference_scenario as rs  # noqa: E402

#: statistics compared on FR (price_summary columns)
STATS = ("mean_spot", "median_spot", "p10_spot", "p90_spot", "max_spot", "min_spot")


def ensemble(mc: Path, fp: str, draws: list[int]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(price_summary, negative_zero_hours) of exactly these completed draws, long, with a `draw` column."""
    ps, nz, missing = [], [], []
    for d in draws:
        dd = Path(mc) / f"draw_{d:03d}"
        if not rs.draw_complete(d, fp, mc) or not (dd / "price_summary.csv").exists():
            missing.append(d)
            continue
        ps.append(pd.read_csv(dd / "price_summary.csv").assign(draw=d))
        if (dd / "negative_zero_hours.csv").exists():
            nz.append(pd.read_csv(dd / "negative_zero_hours.csv").assign(draw=d))
    if missing:
        raise SystemExit(f"draws {missing} of the map's ensemble are not complete under {fp}")
    return pd.concat(ps, ignore_index=True), (pd.concat(nz, ignore_index=True) if nz else pd.DataFrame())


def run_stats(run: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(price_summary, negative_zero_hours) of a run, from its deliverables or computed from its parquets."""
    if (run / "price_summary.csv").exists() and (run / "negative_zero_hours.csv").exists():
        return pd.read_csv(run / "price_summary.csv"), pd.read_csv(run / "negative_zero_hours.csv")
    ps, nz = [], []
    for p in sorted(run.glob("prices_*.parquet")):
        y = int(p.stem.split("_")[1])
        spot = pd.read_parquet(p)["spot"]
        for z in spot.columns:
            s = spot[z]
            ps.append({"year": y, "zone": z, "mean_spot": s.mean(), "median_spot": s.median(),
                       "p10_spot": s.quantile(0.10), "p90_spot": s.quantile(0.90),
                       "max_spot": s.max(), "min_spot": s.min()})
            nz.append({"year": y, "zone": z, "hours": len(s), "hours_le_zero_spot": int((s <= 0).sum())})
    if not ps:
        raise SystemExit(f"no prices_*.parquet and no price_summary.csv under {run}")
    return pd.DataFrame(ps), pd.DataFrame(nz)


def compare(sel: dict[int, int], E: pd.DataFrame, R: pd.DataFrame, stat: str, zone: str,
            O: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per year: reference, d(y)'s own, ensemble mean and sd (over E's draws only), and the two deviations. `O` holds
    the own runs of mapped draws outside the ensemble."""
    e = E[E["zone"] == zone].pivot(index="year", columns="draw", values=stat)
    o = e if O is None or O.empty else pd.concat([E, O]).drop_duplicates(["year", "zone", "draw"]).query(
        "zone == @zone").pivot(index="year", columns="draw", values=stat)
    r = R[R["zone"] == zone].set_index("year")[stat]
    rows = []
    for y in sorted(set(r.index) & set(e.index) & set(sel)):
        own, mu, sd = float(o.loc[y, sel[y]]), float(e.loc[y].mean()), float(e.loc[y].std())
        ref = float(r.loc[y])
        rows.append({"year": y, "draw": sel[y], "ref": ref, "own": own, "ens_mean": mu, "ens_sd": sd,
                     "ref_minus_own": ref - own, "ref_minus_ens": ref - mu, "own_minus_ens": own - mu,
                     "z": (ref - mu) / sd if sd > 0 else float("nan")})
    return pd.DataFrame(rows).set_index("year") if rows else pd.DataFrame()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True, help="the reference run's directory (e.g. reports/sens/s00_baseline)")
    ap.add_argument("--mc", default=str(rs.MC_OUT))
    ap.add_argument("--zone", default="FR")
    ap.add_argument("--json", default=None,
                    help="default: <ref>/reference_check.json for FR, <ref>/reference_check_<zone>.json otherwise")
    a = ap.parse_args()

    run = Path(a.ref)
    sel, meta = rs.load_map()
    fp, draws = meta["model_fingerprint"], [int(d) for d in meta["draws"]]
    stamp = run / "reference_run.json"
    if not stamp.exists():
        raise SystemExit(f"{stamp} absent: not a run of scripts/run_reference_20y.py, nothing to check it against")
    ident = json.loads(stamp.read_text(encoding="utf-8")).get("identity", {})
    if (ident.get("map_meta_sha256") != rs.file_sha256(rs.META_PATH)
            or {int(k): int(v) for k, v in ident.get("year_to_draw", {}).items()} != sel):
        raise SystemExit(f"{run} was run on another year map than {rs.MAP_PATH}")

    E, EN = ensemble(Path(a.mc), fp, draws)
    outside = sorted(set(sel.values()) - set(draws))
    O, ON = ensemble(Path(a.mc), fp, outside) if outside else (pd.DataFrame(), pd.DataFrame())
    R, RN = run_stats(run)
    years = sorted(set(R["year"]) & set(sel))
    print(f"reference run {run}: {len(years)} year(s) {years[0]}..{years[-1]}; ensemble {fp}, "
          f"{len(draws)} draws; map from {len(set(sel.values()))} draws, fr_tol {meta.get('fr_tol_eur_mwh')}\n")

    pd.set_option("display.width", 220)
    out: dict = {"ref": str(run), "zone": a.zone, "checker_sha256": rs.file_sha256(Path(__file__)),
                 "model_fingerprint": fp, "ensemble_draws": draws, "years": years,
                 "map_meta_sha256": rs.file_sha256(rs.META_PATH), "year_to_draw": {str(y): d for y, d in sel.items()},
                 "reference_run_identity": ident,
                 "checked_utc": datetime.now(UTC).isoformat(timespec="seconds"), "fr": {}, "zones": {}}
    z0 = a.zone
    head = compare(sel, E, R, "mean_spot", z0, O)
    print(f"=== {z0} annual mean spot (EUR/MWh) ===")
    print(head[["draw", "ref", "own", "ens_mean", "ref_minus_own", "ref_minus_ens", "own_minus_ens", "z"]]
          .round(2).to_string())
    hours_own = pd.concat([EN, ON]) if not ON.empty else EN
    for s in STATS:
        c = compare(sel, E, R, s, z0, O)
        if c.empty:
            continue
        if s == "mean_spot" and not RN.empty and not hours_own.empty:
            rh = RN[RN["zone"] == z0].set_index("year")["hours"]
            oh = hours_own[hours_own["zone"] == z0].set_index(["year", "draw"])["hours"]
            c["ref_hours"] = [int(rh.get(y, -1)) for y in c.index]
            c["own_hours"] = [int(oh.get((y, sel[y]), -1)) for y in c.index]
        out["fr"][s] = {
            "mean_abs_ref_minus_own": float(c["ref_minus_own"].abs().mean()),
            "max_abs_ref_minus_own": float(c["ref_minus_own"].abs().max()),
            "mean_abs_ref_minus_ens": float(c["ref_minus_ens"].abs().mean()),
            "max_abs_ref_minus_ens": float(c["ref_minus_ens"].abs().max()),
            "horizon_ref_minus_ens": float(c["ref_minus_ens"].mean()),
            "mean_abs_z": float(c["z"].abs().mean()),
            "years_abs_ref_minus_ens_gt_fr_tol": int((c["ref_minus_ens"].abs()
                                                      > float(meta.get("fr_tol_eur_mwh", 1.0))).sum()),
            "per_year": {str(y): {k: float(v) for k, v in r.items()} for y, r in c.iterrows()}}   # unrounded
    print(f"\n{'statistic':12s} {'|ref-own| mean':>15s} {'max':>7s}   {'|ref-ens| mean':>15s} {'max':>7s} "
          f"{'horizon':>8s} {'mean|z|':>8s}")
    for s, v in out["fr"].items():
        print(f"{s:12s} {v['mean_abs_ref_minus_own']:15.2f} {v['max_abs_ref_minus_own']:7.2f}   "
              f"{v['mean_abs_ref_minus_ens']:15.2f} {v['max_abs_ref_minus_ens']:7.2f} "
              f"{v['horizon_ref_minus_ens']:+8.2f} {v['mean_abs_z']:8.2f}")

    print("\n=== every zone, annual mean spot: reproduction and centre (EUR/MWh) ===")
    print(f"{'zone':10s} {'|ref-own| mean':>15s} {'max':>7s}   {'|ref-ens| mean':>15s} {'max':>7s} "
          f"{'horizon':>8s} {'mean|z|':>8s}")
    for z in sorted(set(R["zone"]) & set(E["zone"])):
        c = compare(sel, E, R, "mean_spot", z, O)
        if c.empty:
            continue
        rec = {"mean_abs_ref_minus_own": float(c["ref_minus_own"].abs().mean()),
               "max_abs_ref_minus_own": float(c["ref_minus_own"].abs().max()),
               "mean_abs_ref_minus_ens": float(c["ref_minus_ens"].abs().mean()),
               "max_abs_ref_minus_ens": float(c["ref_minus_ens"].abs().max()),
               "horizon_ref_minus_ens": float(c["ref_minus_ens"].mean()),
               "mean_abs_z": float(c["z"].abs().mean())}
        out["zones"][z] = rec
        print(f"{z:10s} {rec['mean_abs_ref_minus_own']:15.2f} {rec['max_abs_ref_minus_own']:7.2f}   "
              f"{rec['mean_abs_ref_minus_ens']:15.2f} {rec['max_abs_ref_minus_ens']:7.2f} "
              f"{rec['horizon_ref_minus_ens']:+8.2f} {rec['mean_abs_z']:8.2f}")

    if not EN.empty and not RN.empty and "hours_le_zero_spot" in RN:
        c = compare(sel, EN.assign(hours_le_zero_spot=EN["hours_le_zero_spot"].astype(float)),
                    RN.assign(hours_le_zero_spot=RN["hours_le_zero_spot"].astype(float)), "hours_le_zero_spot", z0,
                    None if ON.empty else ON.assign(hours_le_zero_spot=ON["hours_le_zero_spot"].astype(float)))
        if not c.empty:
            out["fr"]["hours_le_zero_spot"] = {
                "mean_abs_ref_minus_own": float(c["ref_minus_own"].abs().mean()),
                "mean_abs_ref_minus_ens": float(c["ref_minus_ens"].abs().mean()),
                "horizon_ref_minus_ens": float(c["ref_minus_ens"].mean())}
            print(f"\n{z0} hours <= 0 EUR/MWh per year: |ref-own| mean "
                  f"{out['fr']['hours_le_zero_spot']['mean_abs_ref_minus_own']:.0f} h, |ref-ens| mean "
                  f"{out['fr']['hours_le_zero_spot']['mean_abs_ref_minus_ens']:.0f} h, horizon "
                  f"{out['fr']['hours_le_zero_spot']['horizon_ref_minus_ens']:+.0f} h")

    dest = Path(a.json) if a.json else run / ("reference_check.json" if a.zone == "FR"
                                              else f"reference_check_{a.zone}.json")
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=1), encoding="utf-8")
    os.replace(tmp, dest)
    print(f"\nwrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
