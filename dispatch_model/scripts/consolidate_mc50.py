"""Collapse the 50-draw ensemble into ONE workbook.

The archive keeps 800 files because that is the record. This is the thing a person opens: every aggregate
the ensemble produced, indexed by draw, in long form so it pivots. It exists because the archive cannot be
emailed (1.6 GB against a 25 MB cap) and because 800 directories is not a deliverable.

Tabs:
  Notice            what this is, how it was produced, what it does and does not sample
  Prices            draw x year x zone: mean / median / p10 / p90 / max / min spot, hours <= 0, hours > 500
  FR by tech        draw x year x tech: generation TWh, revenue MEUR, capture price, capture rate
  FR capture        draw x year x tech, wide -- the capture-price pivot as shipped per draw
  FR revenue        draw x year x tech, wide
  Congestion        draw x year x border: congestion rent MEUR
  Weather           draw x year: the draw's own weather summary
  Stats             per year across draws: mean / median / p10 / p90 / min / max of FR mean spot
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "reports" / "mc50"
OUT = REPO.parent / "archive"
OUT.mkdir(parents=True, exist_ok=True)

# ONLY DRAWS OF ONE MODEL. A `.draw_complete` carries the fingerprint of the model that wrote it; counting any
# marker would merge two models into one "ensemble" whenever a recompute is under way.
import argparse  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_fingerprint import digest  # noqa: E402

_ap = argparse.ArgumentParser()
_ap.add_argument("--fingerprint", default=None,
                 help="only draws whose marker names this model (default: the model on disk now)")
_args = _ap.parse_args()
FP = _args.fingerprint or digest({"master_seed": 20260629, "n_weeks": None})             # run_montecarlo defaults
_marked = sorted(d for d in SRC.glob("draw_*") if (d / ".draw_complete").exists())
draws = [d for d in _marked if (d / ".draw_complete").read_text(encoding="utf-8").strip() == FP]
if len(draws) < len(_marked):
    print(f"  {len(_marked) - len(draws)} completed draw(s) belong to another model and are left out")
if not draws:
    raise SystemExit(f"no completed draw of model {FP} in {SRC}")
print(f"consolidating {len(draws)} draws of model {FP}")


def gather(fname, wide_value=None):
    out = []
    for d in draws:
        f = d / fname
        if not f.exists():
            continue
        n = int(d.name.split("_")[1])
        t = pd.read_csv(f, index_col=0) if wide_value else pd.read_csv(f)
        if wide_value:
            t = t.reset_index().melt(id_vars=t.index.name or "year",
                                     var_name="tech", value_name=wide_value)
            t = t.rename(columns={t.columns[0]: "year"})
        out.append(t.assign(draw=n))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


prices = gather("price_summary.csv")
# `revenue_by_tech.csv` carries every zone (12 x ~9 techs x 20 y x 50 draws = 103k rows, and 10 of the
# 13 MB this workbook first came to). The tab is labelled FR and the ensemble is read for France, so keep
# France here; the per-zone detail stays in the full archive where it belongs.
bytech = gather("revenue_by_tech.csv")
if not bytech.empty and "zone" in bytech.columns:
    bytech = bytech[bytech["zone"] == "FR"].reset_index(drop=True)
cap = gather("pivot_fr_capture_eurpermwh.csv", wide_value="capture_eur_mwh")
rev = gather("pivot_fr_revenue_meur.csv", wide_value="revenue_meur")
cong = gather("congestion_rent.csv")
wx = gather("weather_summary.csv")

fr = prices[prices["zone"] == "FR"]
stats = (fr.groupby("year")["mean_spot"]
         .agg(mean="mean", median="median",
              p10=lambda s: s.quantile(0.10), p90=lambda s: s.quantile(0.90),
              min="min", max="max", n="count").reset_index().round(2))

notice = pd.DataFrame({"": [
    "PriceModeling - 50-draw Monte-Carlo ensemble, consolidated aggregates",
    "",
    f"draws: {len(draws)} (0..{len(draws)-1}), master_seed 20260629, horizon 2027-2046, 12 bidding zones",
    "",
    "WHAT THIS SAMPLES: weather, and only weather. Each draw is one coherent 20-year weathergen",
    "trajectory carried through demand (iii), RES (iv), plant availability (v) and dispatch (vi/vii),",
    "so the spread here is weather spread.",
    "",
    "WHAT IT DOES NOT SAMPLE: commodity prices, capacity trajectories, demand scenarios, policy. Every",
    "draw uses the same REFERENCE_SCENARIOS assumptions. Structural uncertainty is NOT in these numbers.",
    "",
    "KNOWN LIMITATIONS carried by every draw:",
    " - GB is over-coupled to the continent. Its modelled price exactly equals a neighbour's in 34-45 % of",
    "   hours against 0.1 % observed; GB is HVDC-connected and does not converge the way AC-coupled zones",
    "   do. This inflates GB's cheap-hour count and leaks into BE/NL/FR.",
    " - Nuclear outage sampling is ONE realisation per weather draw, so the volume spread (and hence the",
    "   revenue spread) is narrower than reality. Load-factor variation here is weather-driven only.",
    " - Backtest gate at the time of this ensemble: pooled |mean error| 11.3 EUR/MWh, scarcity recall",
    "   34280/34917. log_err 1.42 against a 1.34 baseline -- levels improved, hour-by-hour ratio fit did not.",
    "",
    "Full archive (all 800 files incl. hourly series) and a SHA-256 manifest are held alongside this file.",
]})

xl = OUT / "mc50_consolidated.xlsx"
with pd.ExcelWriter(xl, engine="openpyxl") as w:
    notice.to_excel(w, sheet_name="Notice", index=False)
    stats.to_excel(w, sheet_name="Stats", index=False)
    prices.to_excel(w, sheet_name="Prices", index=False)
    bytech.to_excel(w, sheet_name="FR by tech", index=False)
    cap.to_excel(w, sheet_name="FR capture", index=False)
    rev.to_excel(w, sheet_name="FR revenue", index=False)
    cong.to_excel(w, sheet_name="Congestion", index=False)
    wx.to_excel(w, sheet_name="Weather", index=False)
print(f"{xl.name}: {xl.stat().st_size/1e6:.2f} MB")
for name, t in (("Prices", prices), ("FR by tech", bytech), ("Congestion", cong), ("Weather", wx)):
    print(f"  {name:12s} {len(t):7d} rows")
