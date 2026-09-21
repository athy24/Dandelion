"""Collapse the sensitivity set into ONE workbook, read as DELTAS against the baseline.

A sensitivity is not interesting in levels — the level is the baseline's, and every variant reproduces it
to within the one thing that changed. What is interesting is the difference, so every table here carries
the variant's own number AND its delta against `s00_baseline`, which shares the same weather, the same
code and the same everything else by construction.

Tabs:
  Notice           what the set is, what it varies, what it cannot say
  FR summary       variant x year: FR mean spot, delta, % delta -- the headline
  FR nuclear       variant x year: nuclear capture price, revenue, generation, each with its delta
  All zones        variant x year x zone: mean spot and delta
  Negative hours   variant x year x zone: hours <= 0 and delta
  Congestion       variant x year x border: congestion rent and delta
  Ranking          mean over the horizon of the FR delta, ordered -- which assumption matters most
  Baseline gate    per year: the re-run baseline against its draw's own run and the ensemble mean (FR)

Runs on whatever is complete, and says how many that is; it does not wait for the whole set. Complete means
complete UNDER THIS MODEL: a variant counts only when its `.sens_complete` names the fingerprint
`run_sensitivities.variant_fingerprints` gives it now, so results of an earlier model or reference that have not
yet been moved aside are left out, and named. The Notice reports the baseline gate (reference_gate.py) -- whether it
passed, the worst, mean and horizon gaps, and on a failure the years that failed -- and the Baseline gate tab gives
every year. The demand figures of the Notice are computed from the measured reference (fr_demand_ref.json) and the
variants' own definitions (make_sensitivities.DEMAND_REDUCTIONS), not typed in.

    python -X utf8 -W ignore scripts/consolidate_sensitivities.py
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
ROOT = REPO.parent
SRC = REPO / "reports" / "sens"
OUT = ROOT / "archive"
BASE = "s00_baseline"


def gather(fname: str, dirs: dict[str, Path]) -> pd.DataFrame:
    out = []
    for vid, d in dirs.items():
        f = d / fname
        if f.exists():
            out.append(pd.read_csv(f).assign(variant=vid))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def delta(df: pd.DataFrame, keys: list[str], cols: list[str]) -> pd.DataFrame:
    """Join each variant's rows to the baseline's on `keys` and add `<col>_delta` (and `_pct` for prices)."""
    if df.empty or BASE not in set(df["variant"]):
        return df
    b = df[df["variant"] == BASE].drop(columns=["variant"])
    m = df.merge(b, on=keys, suffixes=("", "_base"), how="left")
    for c in cols:
        if c in m and f"{c}_base" in m:
            m[f"{c}_delta"] = m[c] - m[f"{c}_base"]
            m[f"{c}_pct"] = (m[c] / m[f"{c}_base"] - 1.0) * 100.0
            m = m.drop(columns=[f"{c}_base"])
    return m


def main() -> int:
    global SRC, OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SRC), help="directory of completed variant runs")
    ap.add_argument("--out", default=str(OUT), help="where the workbook lands")
    ap.add_argument("--start", type=int, default=2027, help="the horizon the set was run on")
    ap.add_argument("--end", type=int, default=2046)
    ap.add_argument("--n-weeks", type=int, default=None, help="only for a smoke set run with --n-weeks")
    a = ap.parse_args()
    SRC, OUT = Path(a.src), Path(a.out)
    man = {v["id"]: v for v in
           json.loads((ROOT / "archive" / "sensitivities" / "manifest.json").read_text(encoding="utf-8"))}
    # a variant counts only under the model and reference weather that the runner would key it on now
    sys.path.insert(0, str(REPO / "scripts"))
    import run_sensitivities as rs
    if not rs.CUBE.exists():
        raise SystemExit(f"reference weather absent: {rs.CUBE} - no variant can be matched to a model")
    dirs = rs.complete_variants(SRC, rs.variant_fingerprints(list(man.values()), a.start, a.end, a.n_weeks))
    marked = sorted(vid for vid in man if (SRC / vid / ".sens_complete").exists())
    if len(dirs) < len(marked):
        print(f"  left out, marked under another model or reference weather: {sorted(set(marked) - set(dirs))}")
    print(f"complete variants: {len(dirs)}/{len(man)}  {sorted(dirs)}")
    if BASE not in dirs:
        print(f"WARNING: {BASE} is not complete — deltas cannot be computed, levels only")

    prices = gather("price_summary.csv", dirs)
    negs = gather("negative_zero_hours.csv", dirs)
    cong = gather("congestion_rent.csv", dirs)
    bytech = gather("revenue_by_tech.csv", dirs)

    fr = prices[prices["zone"] == "FR"] if not prices.empty else prices
    fr_sum = delta(fr[["variant", "year", "mean_spot", "median_spot"]].copy(),
                   ["year"], ["mean_spot", "median_spot"]) if not fr.empty else fr

    nuc = pd.DataFrame()
    if not bytech.empty:
        n = bytech[(bytech.get("zone") == "FR") & (bytech["tech"] == "nuclear")].copy()
        keep = [c for c in ("variant", "year", "generation_TWh", "revenue_MEUR_spot",
                            "capture_price_spot", "capture_rate_spot") if c in n]
        nuc = delta(n[keep].copy(), ["year"],
                    [c for c in keep if c not in ("variant", "year")])

    allz = delta(prices[["variant", "year", "zone", "mean_spot"]].copy(),
                 ["year", "zone"], ["mean_spot"]) if not prices.empty else prices
    ncol = next((c for c in ("hours_le_zero_spot", "hours_lt_zero_spot") if c in negs), None)
    negd = delta(negs.copy(), ["year", "zone"], [ncol]) if (not negs.empty and ncol) else negs
    ccol = next((c for c in ("congestion_rent_MEUR_spot", "congestion_rent_MEUR_smc") if c in cong), None)
    congd = delta(cong.copy(), ["year", "border"], [ccol]) if (not cong.empty and ccol) else cong

    rank = pd.DataFrame()
    if not fr_sum.empty and "mean_spot_delta" in fr_sum:
        rank = (fr_sum[fr_sum["variant"] != BASE]
                .groupby("variant")
                .agg(mean_delta_eur_mwh=("mean_spot_delta", "mean"),
                     max_delta_eur_mwh=("mean_spot_delta", lambda s: s.loc[s.abs().idxmax()]),
                     mean_pct=("mean_spot_pct", "mean"))
                .reset_index())
        rank["label"] = rank["variant"].map(lambda v: man[v]["label"])
        rank = rank.reindex(rank["mean_delta_eur_mwh"].abs().sort_values(ascending=False).index)
        rank = rank[["variant", "label", "mean_delta_eur_mwh", "max_delta_eur_mwh", "mean_pct"]].round(2)

    import reference_gate as rgate
    import reference_scenario as refsc
    sel, meta = refsc.load_map()
    fps = rs.variant_fingerprints(list(man.values()), a.start, a.end, a.n_weeks)
    stamp = rgate.load(SRC / BASE, fps[BASE]) if BASE in fps else None
    gate_tab = pd.DataFrame()
    if stamp is None:
        gate_lines = ["THE BASELINE GATE HAS NOT BEEN PASSED for this reference: no current verdict on s00_baseline."]
        print("WARNING: no current baseline gate verdict - the set should not have been run")
    else:
        gaps = {int(y): float(g) for y, g in stamp["gaps"].items()}
        stats = (f"worst |gap| {stamp['max_abs_gap']:.2f} EUR/MWh, mean |gap| "
                 f"{sum(abs(g) for g in gaps.values()) / max(len(gaps), 1):.2f}, horizon "
                 f"{sum(gaps.values()) / max(len(gaps), 1):+.2f}")
        if stamp["passed"]:
            gate_lines = [f"BASELINE GATE PASSED: in every year the re-run baseline's FR annual mean spot is within "
                          f"{stamp['tolerance_eur_mwh']:.0f} EUR/MWh of the mean of draws {meta['draws'][0]}-"
                          f"{meta['draws'][-1]} ({stats}; per year in the Baseline gate tab)."]
        else:
            gate_lines = [f"BASELINE GATE FAILED: {stats}. Years beyond {stamp['tolerance_eur_mwh']:.0f} EUR/MWh: "
                          f"{stamp.get('failing_years') or 'none'}; hours mismatch: "
                          f"{stamp.get('hours_mismatch_years') or 'none'}; missing: "
                          f"{stamp.get('missing_years') or 'none'}"
                          + (f"; refused: {stamp['refused']}" if stamp.get("refused") else "")
                          + ". THESE VARIANTS SHOULD NOT BE READ AGAINST THIS BASELINE."]
        ck = SRC / BASE / rgate.CHECK
        if ck.exists():
            py = json.loads(ck.read_text(encoding="utf-8"))["fr"]["mean_spot"]["per_year"]
            gate_tab = pd.DataFrame([{"year": int(y), "draw": int(r["draw"]), "reference_rerun": r["ref"],
                                      "draw_own_run": r["own"], "ensemble_mean": r["ens_mean"],
                                      "rerun_minus_own": r["ref_minus_own"], "rerun_minus_ensemble": r["ref_minus_ens"],
                                      "within_tolerance": abs(r["ref_minus_ens"]) <= stamp["tolerance_eur_mwh"],
                                      "hours": int(r.get("ref_hours", -1))} for y, r in sorted(py.items())])
    adjusted = meta.get("adjustments", [])
    adj_years = sorted({int(h["year"]) for h in adjusted})
    adj_line = (f", and {len(adj_years)} year(s) re-chosen after the baseline gate ("
                + ", ".join(f"{h['year']}: {h['from_draw']}->{h['to_draw']} [{h['tier']}]" for h in adjusted) + ")."
                if adjusted else "; no year had to be re-chosen after the baseline gate.")

    import make_sensitivities as msens
    dref = json.loads(refsc.DEMAND_REF_PATH.read_text(encoding="utf-8"))["fr_demand_twh"]
    dref = {int(y): float(v) for y, v in dref.items()}
    y1 = max(dref)
    cuts = [(red, msens.demand_path(red, dref)[2]) for _vid, red in msens.DEMAND_REDUCTIONS]
    lvl, lyr = msens.FR_DEMAND_REF_LEVEL, msens.FR_DEMAND_REF_YEAR
    demand_lines = textwrap.wrap(
        f"THE FRENCH DEMAND VARIANTS ARE PERCENTAGE REDUCTIONS OF THE {lvl:.0f} TWh REFERENCE. The reference French "
        f"demand is the step-iii projection of the reference scenario: {dref[min(dref)]:.1f} TWh in {min(dref)}, "
        f"{dref[y1]:.1f} TWh in {y1} (1 January-26 December: the weather ends there), {lvl:.0f} TWh in {lyr} -- the "
        f"demand moderation kept as the reference on 2026-09-14. The workbook's dispatch_tyndp demand_twh row, 740 at "
        f"{lyr}, is read for France and never applied. The variants reduce it by "
        + " and ".join(f"{r:.2%}" for r, _ in cuts) + f" in {lyr} ("
        + " and ".join(f"{(1 - r) * lvl:.1f}" for r, _ in cuts) + " TWh), the proportions 700/745 and 650/745 of the "
        f"original request, the reduction ramping linearly from nothing in {min(dref)}: "
        + " and ".join(f"-{c:.2%}" for _, c in cuts) + f" in {y1}, the last modelled year.", 104)
    notice = pd.DataFrame({"": [
        "PriceModeling - sensitivity set on the REFERENCE scenario",
        "",
        f"variants complete: {len(dirs)}/{len(man)}; horizon 2027-2046; 12 bidding zones; baseline {BASE}",
        "",
        "WHAT THIS VARIES: exactly one assumption per variant, against a baseline that is identical in",
        "every other respect - same code, same weather, same workbook but for the cells that variant names.",
        "So a difference here is the assumption. Each variant's own edits are in its meta.json and in",
        "archive/sensitivities/manifest.json.",
        "",
        *textwrap.wrap(
            "THE REFERENCE IS THE ENSEMBLE'S CENTRE, YEAR BY YEAR. Each year y is taken whole from one Monte-Carlo "
            f"draw d(y) of ensemble {meta['model_fingerprint']} ({len(set(sel.values()))} draws over 20 years): its "
            "French demand/RES series, its nuclear and reservoir availability and its neighbours' weather. The draws "
            f"were chosen on draws {meta['draws'][0]}-{meta['draws'][-1]} (FR annual mean within 1 EUR/MWh of their "
            "mean, then the neighbours closest to theirs)" + adj_line, 104),
        *[w for g in gate_lines for w in textwrap.wrap(g, 104)],
        "It is NOT a physically sampled 20-year sequence: consecutive years come from different draws, so",
        "year-to-year weather persistence is broken by construction. Read multi-year risk from the ensemble.",
        "",
        "TWO CONVENTIONS, chosen and not deduced:",
        " - commodity variants move the LONG-RUN anchors only (>= 2031); 2027-2030 are quoted forwards.",
        " - RES capacity variants move the PROJECTED years only (>= 2027); 2025-2026 are already built.",
        "",
        *demand_lines,
        "",
        "ONE VARIANT CANNOT BE READ AS NAMED. The neighbour model carries a single wind aggregate and a",
        "single blended RES growth factor, so 'non-FR solar +10 %' and 'non-FR wind +10 %' both apply a",
        "UNIFORM uplift to the same must-take series, differing only in size. Neither tilts the profile",
        "toward midday or toward windy hours. Read them as 'more neighbour renewables, by this much",
        "energy'.",
        "",
        "KNOWN LIMITATIONS carried by every variant, as in the ensemble:",
        " - GB is over-coupled to the continent (its price equals a neighbour's in 34-45 % of hours",
        "   against 0.1 % observed), which inflates its cheap-hour count and leaks into BE/NL/FR.",
        " - Nuclear outage sampling is ONE realisation, so volume spread is narrower than reality.",
        " - Backtest gate: pooled |mean error| 11.3 EUR/MWh, scarcity recall 34280/34917, log_err 1.42.",
    ]})

    OUT.mkdir(parents=True, exist_ok=True)
    xl = OUT / "sensitivities_consolidated.xlsx"
    with pd.ExcelWriter(xl, engine="openpyxl") as w:
        notice.to_excel(w, sheet_name="Notice", index=False)
        if not rank.empty:
            rank.to_excel(w, sheet_name="Ranking", index=False)
        if not gate_tab.empty:
            gate_tab.round(3).to_excel(w, sheet_name="Baseline gate", index=False)
        for name, t in (("FR summary", fr_sum), ("FR nuclear", nuc), ("All zones", allz),
                        ("Negative hours", negd), ("Congestion", congd)):
            if t.empty:
                continue
            if "variant" in t:                      # the tab is read variant-by-variant; lead with it
                t = t[["variant"] + [c for c in t.columns if c != "variant"]]
            t.round(3).to_excel(w, sheet_name=name, index=False)
    print(f"{xl.name}: {xl.stat().st_size/1e6:.2f} MB")
    if not rank.empty:
        print("\nFR mean spot, average delta over the horizon (EUR/MWh):")
        print(rank.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
