"""The 50-draw weather boxplots, with the completed sensitivity runs drawn over them.

The box is WEATHER SPREAD: fifty draws, one set of assumptions. The lines are ASSUMPTION SPREAD: one
weather trajectory (REFERENCE_WEATHER_20Y, the ensemble's own per-year centre), one assumption changed.
They answer different questions and the chart only works if that is legible, so the baseline run is drawn
too -- a sensitivity read against the box median is read against a different weather, and the difference
between s00 and that median is itself worth seeing.

    python -u -X utf8 -W ignore scripts/mk_sens_overlay_charts.py [--out scratchpad]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MC = REPO / "reports" / "mc50"
SENS = REPO / "reports" / "sens"

#: the runs drawn as lines, in legend order: (variant id, label, colour, linestyle, width)
OVERLAY = [
    ("s00_baseline", "Reference assumptions", "#12455f", "-", 2.4),
    ("s01_co2_175", "CO2 175 EUR/t", "#c2553f", "--", 2.0),
    ("s02_co2_150", "CO2 150 EUR/t", "#8c1d13", "-.", 2.0),
]

#: chart key -> (title, y label, how to pull the per-year value from a run's CSVs)
CHARTS = {
    "mean":               ("yearly MEAN spot price", "EUR/MWh"),
    "median":             ("yearly MEDIAN spot price", "EUR/MWh"),
    "nuclear_capture":    ("nuclear CAPTURE price", "EUR/MWh"),
    "nuclear_revenue":    ("nuclear REVENUE", "bn EUR/year"),
    "nuclear_generation": ("nuclear GENERATION", "TWh/year"),
}


def series_from(run_dir: Path) -> dict[str, dict[int, float]]:
    """The five metrics for one completed run, per year."""
    px = pd.read_csv(run_dir / "price_summary.csv")
    fr = px[px["zone"] == "FR"].set_index("year")
    bt = pd.read_csv(run_dir / "revenue_by_tech.csv")
    nu = bt[(bt["zone"] == "FR") & (bt["tech"] == "nuclear")].set_index("year")
    return {
        "mean": fr["mean_spot"].to_dict(),
        "median": fr["median_spot"].to_dict(),
        "nuclear_capture": nu["capture_price_spot"].to_dict(),
        "nuclear_revenue": (nu["revenue_MEUR_spot"] / 1000.0).to_dict(),
        "nuclear_generation": nu["generation_TWh"].to_dict(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "scratchpad"))
    ap.add_argument("--fingerprint", default=None,
                    help="only draws whose marker names this model (default: the model on disk now)")
    ap.add_argument("--start", type=int, default=2027, help="the horizon the sensitivities were run on")
    ap.add_argument("--end", type=int, default=2046)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from model_fingerprint import digest
    fp = a.fingerprint or digest({"master_seed": 20260629, "n_weeks": None})       # run_montecarlo defaults
    marked = sorted(d for d in MC.glob("draw_*") if (d / ".draw_complete").exists())
    draws = [d for d in marked if (d / ".draw_complete").read_text(encoding="utf-8").strip() == fp]
    if len(draws) < len(marked):
        print(f"  {len(marked) - len(draws)} completed draw(s) belong to another model and are left out")
    print(f"ensemble: {len(draws)} draws of model {fp}")
    ens: dict[str, dict[int, list]] = {k: {} for k in CHARTS}
    for d in draws:
        s = series_from(d)
        for k, per_year in s.items():
            for y, v in per_year.items():
                ens[k].setdefault(int(y), []).append(float(v))

    # the lines obey the boxes' rule: a variant counts only when its marker names this model and reference weather
    import json

    import run_sensitivities as rs
    sens_ok = {}
    if rs.CUBE.exists() and rs.MANIFEST.exists():
        variants = json.loads(rs.MANIFEST.read_text(encoding="utf-8"))
        sens_ok = rs.complete_variants(SENS, rs.variant_fingerprints(variants, a.start, a.end, None))
    runs = []
    for vid, label, colour, ls, lw in OVERLAY:
        rd = SENS / vid
        if vid not in sens_ok:
            print(f"  skip {vid}: not complete under this model and reference weather")
            continue
        runs.append((vid, label, colour, ls, lw, series_from(rd)))
    print(f"overlays: {[r[0] for r in runs]}")

    years = sorted(ens["mean"])
    n = len(draws)
    for key, (title, ylab) in CHARTS.items():
        data = [ens[key][y] for y in years]
        fig, ax = plt.subplots(figsize=(13.0, 6.2), dpi=170)
        fig.patch.set_facecolor("white")
        ax.set_facecolor("white")
        ax.boxplot(data, positions=range(len(years)), widths=0.55, patch_artist=True,
                   medianprops={"color": "#12455f", "lw": 1.6},
                   boxprops={"facecolor": "#3d7ea6", "alpha": 0.28, "edgecolor": "#2b6d92", "lw": 1.1},
                   whiskerprops={"color": "#2b6d92", "lw": 1.0},
                   capprops={"color": "#2b6d92", "lw": 1.0},
                   flierprops={"marker": "o", "ms": 3.5, "mfc": "#7a8590", "mec": "none", "alpha": 0.6})
        for _vid, label, colour, ls, lw, s in runs:
            v = [s[key].get(y, np.nan) for y in years]
            ax.plot(range(len(years)), v, color=colour, lw=lw, ls=ls, label=label,
                    marker="o", ms=3.2, mfc=colour, mec="white", mew=0.6, zorder=5)
        ax.set_xticks(range(len(years)))
        ax.set_xticklabels([str(y) for y in years], rotation=45, ha="right", fontsize=9.5)
        ax.set_ylabel(ylab, fontsize=10.5)
        ax.set_title(f"France - {title}", fontsize=12.5, weight="600", loc="left", pad=10)
        ax.text(0, 1.015, f"boxes: {n} Monte-Carlo weather draws, reference assumptions   |   "
                          f"lines: single runs on REFERENCE_WEATHER_20Y",
                transform=ax.transAxes, fontsize=8.8, color="#5b6672")
        ax.grid(axis="y", color="#7a8590", alpha=0.22, lw=0.9)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color("#9aa6b2")
        ax.tick_params(colors="#5b6672", labelsize=9.5)
        leg = ax.legend(loc="best", frameon=True, fontsize=9.5, framealpha=0.92)
        leg.get_frame().set_edgecolor("#d3dae1")
        fig.tight_layout()
        p = out / f"fr_{key}_sens.jpg"
        fig.savefig(p, format="jpg", pil_kwargs={"quality": 94}, facecolor="white")
        plt.close(fig)
        print(f"wrote {p.name}  {p.stat().st_size // 1024} KB")

    # the numbers behind the lines, so the chart can be read without a ruler
    tbl = []
    for key in CHARTS:
        for y in years:
            row = {"metric": key, "year": y,
                   "ens_median": float(np.median(ens[key][y])),
                   "ens_p10": float(np.quantile(ens[key][y], 0.10)),
                   "ens_p90": float(np.quantile(ens[key][y], 0.90))}
            for vid, _l, _c, _ls, _lw, s in runs:
                row[vid] = s[key].get(y, np.nan)
            tbl.append(row)
    t = pd.DataFrame(tbl)
    t.to_csv(out / "fr_sens_overlay.csv", index=False)
    print(f"wrote fr_sens_overlay.csv ({len(t)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
