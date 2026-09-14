"""Does a run on REFERENCE_WEATHER_20Y actually reproduce the 50-draw ensemble's central prices?

REFERENCE_WEATHER_20Y was built by taking, for each year, the weather of the draw whose FR annual MEAN spot
sat closest to the ensemble mean for that year. That selection is a fact about the ARCHIVE: draw d(y)'s own
run priced at the ensemble mean in year y, to within 0.47 EUR/MWh. It is NOT a claim about a re-run.

A re-run differs from draw d(y)'s run in ways the selection never controlled:

  * the step-v availability partition is `availability_mc/wdraw=900` -- ONE realisation projected over the
    stitched cube -- where draw d(y) used `wdraw=d(y)`. Both the nuclear availability fraction and the
    weekly reservoir energy budget therefore belong to a different draw from the weather.
  * the 1-January seam taper rewrote 72 h either side of each year boundary, and those are winter hours.
  * lagged demand features at the start of each year see the PREVIOUS year's weather, from another draw.

So the fidelity has to be MEASURED, not assumed. This compares a reference run -- however many years of it
exist -- against the ensemble, statistic by statistic, and says where it lands inside the ensemble's own
spread. A deviation is reported in EUR/MWh and, more usefully, as a z-score against the between-draw sd:
half a standard deviation is a different answer from half a euro.

    python -X utf8 -W ignore scripts/check_reference_weather.py [--ref scratchpad/refcheck]

Expects the reference run's `price_summary.csv` (build_projection_deliverables writes it) alongside its
parquets; falls back to computing the same statistics straight from `prices_<y>.parquet` when it is absent,
using the deliverables' own definitions so the two are comparable.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
ROOT = REPO.parent

#: statistics compared, and whether a gap is naturally read in EUR/MWh or in hours
STATS = ("mean_spot", "median_spot", "p10_spot", "p90_spot", "max_spot", "min_spot")


def ensemble(src: Path, fp: str) -> pd.DataFrame:
    """Every draw completed by model `fp`: its price_summary, long. A draw of another model, or one still being
    recomputed, is left out -- a reference run compared against a mixed ensemble would measure nothing."""
    out, other = [], 0
    for d in sorted(src.glob("draw_*")):
        m, f = d / ".draw_complete", d / "price_summary.csv"
        if not m.exists():
            continue
        if m.read_text(encoding="utf-8").strip() != fp:
            other += 1
            continue
        if f.exists():
            out.append(pd.read_csv(f).assign(draw=int(d.name.split("_")[1])))
    if other:
        print(f"  {other} completed draw(s) belong to another model and are left out")
    if not out:
        raise SystemExit(f"no draw of model {fp} under {src}")
    return pd.concat(out, ignore_index=True)


def reference(ref: Path) -> pd.DataFrame:
    """The reference run's per-year, per-zone price statistics."""
    f = ref / "price_summary.csv"
    if f.exists():
        return pd.read_csv(f)
    rows = []
    for p in sorted(ref.glob("prices_*.parquet")):
        y = int(p.stem.split("_")[1])
        px = pd.read_parquet(p)
        spot, smc = px["spot"], px["smc"]
        for z in spot.columns:
            s = spot[z]
            rows.append({"year": y, "zone": z, "mean_spot": s.mean(), "median_spot": s.median(),
                         "p10_spot": s.quantile(0.10), "p90_spot": s.quantile(0.90),
                         "max_spot": s.max(), "min_spot": s.min(),
                         "mean_smc": smc[z].mean(),
                         "hours_le_zero_spot": int((s <= 0).sum()),
                         "hours_gt_500_spot": int((s > 500).sum())})
    if not rows:
        raise SystemExit(f"no prices_*.parquet and no price_summary.csv under {ref}")
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=str(REPO / "scratchpad" / "refcheck"))
    ap.add_argument("--mc", default=str(REPO / "reports" / "mc50"))
    ap.add_argument("--zone", default="FR")
    ap.add_argument("--fingerprint", default=None,
                    help="only draws whose marker names this model (default: the model on disk now)")
    a = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from model_fingerprint import digest
    fp = a.fingerprint or digest({"master_seed": 20260629, "n_weeks": None})       # run_montecarlo defaults
    E, R = ensemble(Path(a.mc), fp), reference(Path(a.ref))
    sel = {int(k): int(v) for k, v in json.loads(
        (REPO / "scratchpad" / "ref_weather_map.json").read_text(encoding="utf-8")).items()}
    years = sorted(set(R["year"]) & set(E["year"]))
    print(f"reference years available: {years}")
    print(f"ensemble draws: {E['draw'].nunique()}\n")

    # ---- headline: the requested zone, every statistic ------------------------------------------------
    e, r = E[E.zone == a.zone], R[R.zone == a.zone].set_index("year")
    print(f"=== {a.zone}: reference run against the ensemble ===")
    hdr = f"{'year':<5s} {'draw':>4s} " + "".join(f"{s.replace('_spot',''):>26s}" for s in STATS)
    print(hdr)
    print(f"{'':<5s} {'':>4s} " + "".join(f"{'ref   ens    dev    z':>26s}" for _ in STATS))
    rows = []
    for y in years:
        g = e[e.year == y]
        line = f"{y:<5d} {sel.get(y, 0):>4d} "
        rec = {"year": y}
        for s in STATS:
            if s not in r.columns or s not in g.columns:
                line += f"{'-':>26s}"
                continue
            ref_v = float(r.loc[y, s])
            mu, sd = float(g[s].mean()), float(g[s].std())
            dev = ref_v - mu
            z = dev / sd if sd > 0 else float("nan")
            rec[s] = (ref_v, mu, dev, z)
            line += f"{ref_v:7.1f}{mu:7.1f}{dev:+7.1f}{z:+6.1f}"
        rows.append(rec)
        print(line)

    print()
    for s in STATS:
        d = [rec[s][2] for rec in rows if s in rec]
        z = [rec[s][3] for rec in rows if s in rec]
        if not d:
            continue
        print(f"{s:14s} mean |dev| {np.mean(np.abs(d)):7.2f}  worst |dev| {np.max(np.abs(d)):7.2f}"
              f"   mean z {np.mean(z):+5.2f}  worst |z| {np.max(np.abs(z)):4.2f}"
              f"   |z|>1 in {int(np.sum(np.abs(z) > 1))}/{len(z)} years")

    # ---- every zone, on the mean, because the study reports all twelve --------------------------------
    print("\n=== all zones, mean_spot deviation from the ensemble mean (EUR/MWh, z in brackets) ===")
    zones = sorted(set(R["zone"]) & set(E["zone"]))
    print(f"{'zone':<10s}" + "".join(f"{y:>15d}" for y in years))
    worst = []
    for z in zones:
        ez, rz = E[E.zone == z], R[R.zone == z].set_index("year")
        line = f"{z:<10s}"
        for y in years:
            g = ez[ez.year == y]
            if y not in rz.index or g.empty:
                line += f"{'-':>15s}"
                continue
            dev = float(rz.loc[y, "mean_spot"]) - float(g["mean_spot"].mean())
            sd = float(g["mean_spot"].std())
            zz = dev / sd if sd > 0 else float("nan")
            worst.append((abs(zz), z, y, dev))
            line += f"{dev:+9.1f}({zz:+.1f})"[:15].rjust(15)
        print(line)
    worst.sort(reverse=True)
    print("\nlargest deviations across all zones and years:")
    for az, z, y, dev in worst[:8]:
        print(f"  {z:<10s} {y}  {dev:+7.1f} EUR/MWh  ({az:.1f} sd)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
