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

Runs on whatever is complete, and says how many that is; it does not wait for the whole set. Complete means
complete UNDER THIS MODEL: a variant counts only when its `.sens_complete` names the fingerprint
`run_sensitivities.variant_fingerprints` gives it now, so results of an earlier model or reference weather that
have not yet been moved aside are left out, and named.

    python -X utf8 -W ignore scripts/consolidate_sensitivities.py
"""
from __future__ import annotations

import argparse
import json
import sys
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

    notice = pd.DataFrame({"": [
        "PriceModeling - sensitivity set on REFERENCE_WEATHER_20Y",
        "",
        f"variants complete: {len(dirs)}/{len(man)}; horizon 2027-2046; 12 bidding zones; baseline {BASE}",
        "",
        "WHAT THIS VARIES: exactly one assumption per variant, against a baseline that is identical in",
        "every other respect - same code, same weather, same workbook but for the cells that variant names.",
        "So a difference here is the assumption. Each variant's own edits are in its meta.json and in",
        "archive/sensitivities/manifest.json.",
        "",
        "THE WEATHER IS FIXED, AND IS THE ENSEMBLE'S CENTRE. REFERENCE_WEATHER_20Y takes, for each year,",
        "the weather of the 50-draw ensemble member whose French annual mean spot sat closest to the",
        "ensemble mean for that year (worst deviation 0.47 EUR/MWh, most under 0.10). It is NOT a",
        "physically sampled 20-year sequence: consecutive years come from different draws, so year-to-year",
        "weather persistence is broken by construction. Read multi-year weather risk from the ensemble.",
        "",
        "TWO CONVENTIONS, chosen and not deduced:",
        " - commodity variants move the LONG-RUN anchors only (>= 2031); 2027-2030 are quoted forwards.",
        " - RES capacity variants move the PROJECTED years only (>= 2027); 2025-2026 are already built.",
        "",
        "THE FRENCH DEMAND VARIANTS ARE PROPORTIONAL, NOT ABSOLUTE. They were asked for as 'demand rising",
        "to only 700 / 650 TWh instead of 740-750'. Step iii actually projects 556 TWh in 2046, some 160",
        "below the workbook's own stated scenario (dispatch_tyndp demand_twh: 540/616/680 at 2030/35/40,",
        "740 at 2050) - and that row is read for France and NEVER APPLIED, so the stated path has never",
        "touched a French price. Against 556 an absolute 700 would be a 26 % INCREASE, so the variants are",
        "the proportional cuts 700/745 and 650/745 transposed onto the measured path: 523 and 485 TWh in",
        "2046, growth held to 69 % and 34 % of the reference's, ramping from no change in 2027. The 160 TWh",
        "gap is a separate defect under its own investigation; every price in this set and in the 50-draw",
        "ensemble sits on the lower path.",
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
