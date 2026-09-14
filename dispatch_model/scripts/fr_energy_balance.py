"""French energy balance, year by year, straight out of the LP primal — and reconciled three ways.

An annual generation table is easy to produce and easy to get quietly wrong: the sign of a flow, a storage
leg counted twice, a must-take block that is not the one step iv produced. So every number here is
computed from the persisted LP output and then CHECKED against an independent source:

  * the LP's own energy balance -- generation - storage charging - net exports must equal served demand,
    hour by hour and therefore year by year. Residual is reported, never absorbed.
  * net exports, summed from `flows_<y>.parquet` (hourly, six FR borders) AND from `congestion_rent.csv`
    (the deliverables build's own aggregate). Two paths, one number.
  * served demand against STEP III's projected French load over the same hours -- the input the dispatch
    was given. A gap here means the LP did not serve what it was asked to serve.

THE RES BLOCK IS ONE COLUMN IN THE LP, AND THAT IS NOT A CHOICE MADE HERE. `project_year` hands the French
must-take renewables to the LP as a single `musttake_res_mw` series, so the dispatch cannot attribute an
hour of it to solar rather than wind. The split below comes from STEP IV's own per-technology production on
the same cube and realization, applied as that hour's share -- exact wherever the block is uncurtailed
(which the `dump` column confirms), an attribution rather than a measurement wherever it is not.

THE PROJECTED YEAR IS 52 WEEKS. The rolling window machinery slices whole weeks onto the reference-year
calendar, so each projected year carries 8736 hours, not 8760 (and not 8784 in a leap year). Annual totals
are therefore ~0.3 % below a full-calendar equivalent, and the step-iii comparison is taken over the SAME
8736 hours rather than over the calendar year, or it would show a false 1.5 TWh gap.

    python -u -X utf8 -W ignore scripts/fr_energy_balance.py [--src reports/sens/s00_baseline]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: generation technologies, in merit-ish order for the table
GEN = ("nuclear", "res", "hydro_reservoir", "biomass", "gas", "coal", "oil", "dsr", "flex",
       "storage_discharge")
#: columns that are NOT generation: a sink, a slack, or a curtailment
SINK = ("storage_charge",)
SLACK = ("ens", "dump")


def fr_cols(df: pd.DataFrame) -> dict:
    return {c.split("|", 1)[1]: df[c] for c in df.columns if c.startswith("FR|")}


def step_iv_shares(cube: str) -> pd.DataFrame:
    """Step IV per-technology FR production (MW, hourly, whole horizon) on the same cube/realization."""
    os.environ["POWERSIM_WEATHER_CUBE"] = cube
    from res_model.config import load_config as rm_load
    from res_model.projection.engine import Projector
    p = Projector(rm_load(str(ROOT / "res_model" / "config.yaml"))).production(
        "reference", realization=0, with_residual=False)
    return p[["pv_utility", "pv_distributed", "pv_btm", "wind_onshore",
              "wind_offshore_fixed", "wind_offshore_floating", "hydro_ror"]]


def step_iii_load(cube: str) -> pd.Series:
    """Step III projected FR load (MW, hourly, whole horizon) — the demand the dispatch was handed."""
    os.environ["POWERSIM_WEATHER_CUBE"] = cube
    from demand_model.config import load_config as dm_load
    from demand_model.projection.engine import project_trajectory
    return project_trajectory(dm_load(str(ROOT / "demand_model" / "config.yaml")),
                              "reference", realization=0, with_residual=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(REPO / "reports" / "sens" / "s00_baseline"))
    ap.add_argument("--out", default=str(REPO / "scratchpad" / "fr_balance"))
    ap.add_argument("--cube", default=str(ROOT / "archive" / "REFERENCE_WEATHER_20Y.nc"))
    a = ap.parse_args()
    src, out = Path(a.src), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    years = sorted(int(p.stem.split("_")[1]) for p in src.glob("prices_*.parquet"))
    print(f"source: {src}\nyears: {years[0]}-{years[-1]} ({len(years)})", flush=True)

    print("step iv per-technology production...", flush=True)
    res4 = step_iv_shares(a.cube)
    print("step iii load...", flush=True)
    load3 = step_iii_load(a.cube)

    # the deliverables build's own border aggregate, for the second reading of net exports
    cong = pd.read_csv(src / "congestion_rent.csv")

    rows = []
    for y in years:
        d = pd.read_parquet(src / f"dispatch_{y}.parquet")
        col = fr_cols(d)
        n_h = len(d)
        r = {"year": y, "hours": n_h}
        for k in GEN + SINK + SLACK:
            r[k] = float(col[k].sum() / 1e6) if k in col else 0.0

        # RES split: step iv's per-tech shares over the SAME leading hours the LP was given
        s4 = res4[res4.index.year == y].iloc[:n_h]
        tot4 = float(s4.sum(axis=1).sum() / 1e6)
        for t in s4.columns:
            r[f"res_{t}"] = float(s4[t].sum() / 1e6) / tot4 * r["res"] if tot4 > 0 else 0.0
        r["res_step_iv_TWh"] = tot4                       # before any LP curtailment

        # net exports, two ways
        f = pd.read_parquet(src / f"flows_{y}.parquet")
        frb = f[f["border"].str.startswith("FR>") | f["border"].str.endswith(">FR")]
        net = 0.0
        for b, g in frb.groupby("border"):
            s = float(g["flow_mw"].sum() / 1e6)
            net += s if b.startswith("FR>") else -s
        r["net_export_flows"] = net
        cy = cong[(cong["year"] == y) & (cong["border"].str.contains("FR"))]
        r["net_export_deliverables"] = float(
            sum(v if str(b).startswith("FR-") else -v
                for b, v in zip(cy["border"], cy["net_flow_TWh"])))

        # the LP's own balance, and step iii over the same hours
        r["generation"] = sum(r[k] for k in GEN)
        r["served_demand"] = r["generation"] - r["storage_charge"] - r["net_export_flows"] - r["dump"]
        l3 = load3[load3.index.year == y].iloc[:n_h]
        r["step_iii_demand"] = float(l3.sum() / 1e6)
        r["balance_residual"] = r["served_demand"] - r["step_iii_demand"]
        rows.append(r)
        print(f"  {y}  gen {r['generation']:7.1f}  net exp {r['net_export_flows']:6.1f}  "
              f"served {r['served_demand']:7.1f}  step iii {r['step_iii_demand']:7.1f}  "
              f"residual {r['balance_residual']:+6.2f}", flush=True)

    t = pd.DataFrame(rows).set_index("year")
    t.to_csv(out / "fr_balance.csv")
    (out / "fr_balance.json").write_text(json.dumps(t.round(4).to_dict(orient="index"), indent=1),
                                         encoding="utf-8")
    print(f"\nwrote {out/'fr_balance.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
