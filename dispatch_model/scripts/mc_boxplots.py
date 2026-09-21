"""Boxplots across the completed Monte-Carlo draws of ONE model, one PNG per metric.

Metrics come from each draw's shipped deliverables (reports/<ensemble>/draw_<d>/), with the definitions
scripts/mk_sens_overlay_charts.py uses:
  mean / median FR spot         price_summary.csv, zone FR
  nuclear capture price (spot)  revenue_by_tech.csv, FR nuclear, capture_price_spot
  nuclear revenue (spot)        revenue_by_tech.csv, FR nuclear, revenue_MEUR_spot / 1000
A draw counts only when its `.draw_complete` names the fingerprint (default: the one the draws carry, refused if
they carry several).

THE CRE COST OF EXISTING NUCLEAR, on the capture-price chart (red dotted steps). The Commission de regulation de
l'energie estimated the full production cost of the existing French fleet in its report of September 2023, the
estimate in force throughout 2024: 60.7 EUR/MWh over 2026-2030, 59.1 over 2031-2035, 57.3 over 2036-2040, in 2022
euros (no estimate beyond 2040). Its September 2025 update (60.3 EUR2026/MWh for 2026-2028, 63.4 for 2029-2031) is
not drawn. The model's prices are the dispatch's own and the comparison is indicative: a capture price below the
line means the fleet does not earn its CRE full cost from the spot market in that draw-year.

    python -X utf8 -W ignore scripts/mc_boxplots.py [--mc reports/mc50] [--out reports/mc50/boxplots]
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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]

SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8983"
BLUE, BLUE_DARK, GRID, RED = "#2a78d6", "#1c5cab", "#e4e3df", "#d0342c"

#: metric -> (title, unit, label format)
META = {
    "mean_spot":        ("annual MEAN spot price", "€/MWh", "{:.0f}"),
    "median_spot":      ("annual MEDIAN spot price", "€/MWh", "{:.0f}"),
    "nuclear_revenue":  ("nuclear REVENUE (spot)", "bn €/year", "{:.1f}"),
    "nuclear_capture":  ("nuclear CAPTURE price (spot)", "€/MWh", "{:.0f}"),
}

#: CRE, "Coût de production du parc nucléaire existant", September 2023 (EUR2022/MWh), by period
CRE_2023 = ((2026, 2030, 60.7), (2031, 2035, 59.1), (2036, 2040, 57.3))
CRE_LABEL = "CRE full cost of existing nuclear (report of Sept. 2023, €2022/MWh): 60.7 / 59.1 / 57.3"


def completed_draws(mc: Path, fp: str | None) -> tuple[list[Path], str]:
    marks = {}
    for d in sorted(mc.glob("draw_*")):
        m = d / ".draw_complete"
        if m.exists():
            marks[d] = m.read_text(encoding="utf-8").strip()
    if fp is None:
        fps = sorted(set(marks.values()))
        if len(fps) != 1:
            raise SystemExit(f"draws under {mc} carry several fingerprints {fps}: pass --fingerprint")
        fp = fps[0]
    other = [d.name for d, f in marks.items() if f != fp]
    if other:
        print(f"skipped, written by another model: {other}")
    return [d for d, f in marks.items() if f == fp], fp


def gather(draws: list[Path]) -> pd.DataFrame:
    rows = []
    for d in draws:
        n = int(d.name.split("_")[1])
        px = pd.read_csv(d / "price_summary.csv")
        for r in px[px["zone"] == "FR"].itertuples():
            rows += [(n, int(r.year), "mean_spot", float(r.mean_spot)),
                     (n, int(r.year), "median_spot", float(r.median_spot))]
        bt = pd.read_csv(d / "revenue_by_tech.csv")
        for r in bt[(bt["zone"] == "FR") & (bt["tech"] == "nuclear")].itertuples():
            rows += [(n, int(r.year), "nuclear_capture", float(r.capture_price_spot)),
                     (n, int(r.year), "nuclear_revenue", float(r.revenue_MEUR_spot) / 1000.0)]
    return pd.DataFrame(rows, columns=["draw", "year", "metric", "value"])


def _whisker_top(v: np.ndarray) -> float:                          # the 1.5 x IQR rule matplotlib draws
    q1, q3 = np.percentile(v, [25, 75])
    return float(v[v <= q3 + 1.5 * (q3 - q1)].max())


def boxplot(piv: pd.DataFrame, metric: str, n: int, n_total: int, fp: str, out: Path) -> None:
    title, unit, fmt = META[metric]
    years = list(piv.columns)
    data = [piv[y].dropna().to_numpy() for y in years]
    x = np.arange(len(years))

    fig, ax = plt.subplots(figsize=(13.0, 6.2), dpi=170)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.boxplot(data, positions=x, widths=0.55, patch_artist=True, whis=1.5,
               medianprops={"color": BLUE_DARK, "lw": 2.0, "solid_capstyle": "butt"},
               boxprops={"facecolor": BLUE, "alpha": 0.22, "edgecolor": BLUE, "lw": 1.2},
               whiskerprops={"color": BLUE, "lw": 1.1}, capprops={"color": BLUE, "lw": 1.1},
               flierprops={"marker": "o", "ms": 4, "mfc": MUTED, "mec": SURFACE, "mew": 0.6, "alpha": 0.9})
    ax.boxplot(data, positions=x, widths=0.55, whis=1.5, showfliers=False,     # edges at full strength
               medianprops={"visible": False}, whiskerprops={"visible": False},
               capprops={"visible": False}, boxprops={"color": BLUE, "lw": 1.2})
    ax.scatter(x, [v.mean() for v in data], s=34, color=SURFACE, edgecolors=BLUE_DARK, linewidths=1.4, zorder=5)
    for i in (0, len(years) - 1):                                  # selective labels: median, above the whisker
        ax.annotate(fmt.format(float(np.median(data[i]))), xy=(x[i], _whisker_top(data[i])),
                    xytext=(0, 7), textcoords="offset points", ha="center", va="bottom", fontsize=9, color=INK2)

    lo = min(float(np.min(v)) for v in data)
    hi = max(float(np.max(v)) for v in data)
    if metric == "nuclear_capture":
        first = True
        for y0, y1, cost in CRE_2023:
            idx = [i for i, y in enumerate(years) if y0 <= y <= y1]
            if not idx:
                continue
            ax.hlines(cost, x[idx[0]] - 0.45, x[idx[-1]] + 0.45, colors=RED, linestyles=(0, (1.2, 1.6)), lw=2.2,
                      zorder=6, label=CRE_LABEL if first else None)
            lo, hi, first = min(lo, cost), max(hi, cost), False
        ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=RED, handlelength=3.2)

    ax.set_xticks(x)
    ax.set_xticklabels([str(y) for y in years], fontsize=9.5)
    ax.set_ylabel(unit, fontsize=10.5, color=INK2)
    ax.set_title(f"France — {title} across weather draws", fontsize=13, weight="600", loc="left", pad=26, color=INK)
    ax.text(0, 1.02, f"{n} of {n_total} Monte-Carlo draws (model {fp}), reference assumptions  ·  box = IQR, line = "
                     f"median, dot = mean, whiskers = 1.5 × IQR, points = outlying draws  ·  labels = median",
            transform=ax.transAxes, fontsize=8.6, color=INK2)
    ax.grid(axis="y", color=GRID, lw=0.9)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#c9c8c2")
    ax.tick_params(colors=INK2, labelsize=9.5)
    ax.set_xlim(-0.6, len(years) - 0.4)
    span = hi - lo
    ax.set_ylim(lo - 0.08 * span, hi + 0.14 * span)
    fig.tight_layout()
    fig.savefig(out, format="png", facecolor=SURFACE)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mc", default=str(REPO / "reports" / "mc50"))
    ap.add_argument("--out", default=None, help="default: <mc>/boxplots")
    ap.add_argument("--fingerprint", default=None)
    a = ap.parse_args()
    mc = Path(a.mc)
    out = Path(a.out) if a.out else mc / "boxplots"
    out.mkdir(parents=True, exist_ok=True)

    draws, fp = completed_draws(mc, a.fingerprint)
    df = gather(draws)
    n, n_total = df["draw"].nunique(), len(list(mc.glob("draw_*")))
    print(f"model {fp}: {n} draws used")
    summary = []
    for i, metric in enumerate(META, start=1):
        piv = df[df.metric == metric].pivot(index="draw", columns="year", values="value").sort_index(axis=1)
        p = out / f"{i}_fr_{metric}_boxplot.png"
        boxplot(piv, metric, n, n_total, fp, p)
        summary.append(pd.DataFrame({"metric": metric, "year": piv.columns, "n": piv.count().values,
                                     "mean": piv.mean().values, "median": piv.median().values,
                                     "p10": piv.quantile(.1).values, "p90": piv.quantile(.9).values,
                                     "min": piv.min().values, "max": piv.max().values}))
        print(f"wrote {p}")
    s = pd.concat(summary, ignore_index=True)
    s.round(2).to_csv(out / "boxplot_stats.csv", index=False)
    print(f"wrote {out / 'boxplot_stats.csv'} ({len(s)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
