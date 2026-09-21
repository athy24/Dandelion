"""The 'theoretical' cost of hydrogen, per year, for an electrolyser run the optimal flexible way on that year's prices.

For each modelled year y the electrolyser is assumed to face y's hourly French spot prices every year of its 20-year
life. It chooses which hours to run: more hours spread its capital charge over more kilograms; fewer hours keep it to
the cheap (often zero or negative) hours. With the capital charge fixed per MW and the electricity cost additive over
the hours run, the best set of k hours is always the k CHEAPEST, and the optimum is the k that minimises

    LCOH(k) = (C + sum of the k cheapest prices) / (k * 1000 / e)            [EUR/kg]

    C = capex * CRF(wacc, life) * n / 8760        the annual capital charge per MW, prorated to the n hours priced
    CRF = wacc / (1 - (1 + wacc) ** -life)       (2 MEUR/MW at 8 % over 20 years: 0.1019, 203.7 kEUR per MW-year)
    e = kWh per kg                               (55: one MW makes 18.2 kg an hour)

At the optimum the last hour run is priced at or below the average cost per MWh of electricity-equivalent,
LCOH * 1000 / e, and the next hour above it, so the run rule is a price threshold (`threshold`, the price of the last
hour run). Running earns money in negative-price hours, which the formula keeps.

FOR COMPARISON, A FIXED NUMBER OF HOURS (`--fixed-hours`, default 8000 a year): the same formula at k = 8000 * n / 8760,
i.e. the electrolyser runs its 8000 cheapest hours -- the near-baseload operation a project is often financed on, with
the same perfect foresight, so the gap to the optimum is the value of flexibility and nothing else.

WHAT IS AND IS NOT IN IT -- the "theoretical" price the request asks for: capex and wholesale electricity only. No
fixed O&M, stack replacement, grid tariff (TURPE) or taxes, water, compression, storage or transport; no ramping
limits or minimum load; perfect foresight of the year's prices; the electrolyser's own demand does not move them.

PRICES. The reference baseline (reports/sens/s00_baseline, the ensemble's centre year by year) and, for the spread, each
completed draw of the Monte-Carlo ensemble (dispatch_model/scratchpad/mc/<ensemble>/proj_<d>). A year is its priced
hours: 8736, and 8640 in 2046, whose weather ends on 26 December; the capital charge is prorated to them. `hours` in
the outputs counts the hours run out of those priced hours; `hours_per_year` puts it on an 8760-hour year, the basis of
`--fixed-hours`. The Monte-Carlo quantiles are taken on each draw's own result: `mc_flex_*` for the optimum, the band on
the chart, and `mc_fixed_*` for the fixed hours.

    python -X utf8 -W ignore scripts/electrolyser_lcoh.py [--capex 2e6] [--kwh-per-kg 55] [--wacc 0.08] [--life 20]
                                                          [--fixed-hours 8000] [--baseline DIR] [--mc DIR] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]

SURFACE, INK, INK2, GRID, AXIS, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#c9c8c2", "#8a8983"
BLUE, BLUE_LIGHT, ORANGE = "#2a78d6", "#b7d3f6", "#eb6834"


def crf(wacc: float, life: int) -> float:
    """Capital recovery factor: the constant annual payment that repays 1 over `life` years at `wacc`."""
    return 1.0 / life if wacc == 0 else wacc / (1.0 - (1.0 + wacc) ** -life)


def optimum(prices: np.ndarray, annual_charge: float, kwh_per_kg: float, fixed_hours: float = 8000.0) -> dict:
    """The cost-minimising run for one year's hourly prices, and the run of its `fixed_hours` cheapest hours a year."""
    p = np.sort(np.asarray(prices, dtype=float))
    n = len(p)
    c = annual_charge * n / 8760.0
    k = np.arange(1, n + 1)
    cum = np.cumsum(p)
    lcoh = (c + cum) * kwh_per_kg / (1000.0 * k)
    i = int(np.argmin(lcoh))
    kg = (i + 1) * 1000.0 / kwh_per_kg
    j = min(n, max(1, round(fixed_hours * n / 8760.0))) - 1
    kg_j = (j + 1) * 1000.0 / kwh_per_kg
    return {"lcoh": float(lcoh[i]), "hours": i + 1, "load_factor": (i + 1) / n, "threshold": float(p[i]),
            "avg_price_paid": float(cum[i] / (i + 1)), "capex_part": c / kg, "power_part": float(cum[i]) / kg,
            "fixed_lcoh": float(lcoh[j]), "fixed_capex_part": c / kg_j, "fixed_power_part": float(cum[j]) / kg_j,
            "fixed_avg_price_paid": float(cum[j] / (j + 1)), "baseload_lcoh": float(lcoh[-1]), "priced_hours": n}


def fr_spot(path: Path) -> np.ndarray:
    names = pq.ParquetFile(path).schema_arrow.names
    col = next(c for c in names if c in ("('spot', 'FR')", "spot|FR", "spot_FR"))
    return pq.read_table(path, columns=[col]).column(0).to_numpy()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capex", type=float, default=2.0e6, help="EUR per MW of electrolyser input")
    ap.add_argument("--kwh-per-kg", type=float, default=55.0)
    ap.add_argument("--wacc", type=float, default=0.08)
    ap.add_argument("--life", type=int, default=20)
    ap.add_argument("--fixed-hours", type=float, default=8000.0, help="the fixed-hours comparison, hours a year")
    ap.add_argument("--baseline", default=str(REPO / "reports" / "sens" / "s00_baseline"))
    ap.add_argument("--mc", default=str(REPO / "scratchpad" / "mc" / "mc50-3724001c75e0"))
    ap.add_argument("--out", default=str(REPO / "reports" / "sens" / "charts"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    charge = a.capex * crf(a.wacc, a.life)
    print(f"capex {a.capex / 1e6:.2f} MEUR/MW, CRF {crf(a.wacc, a.life):.4f} -> {charge / 1e3:.1f} kEUR/MW-yr; "
          f"{a.kwh_per_kg:g} kWh/kg = {1000 / a.kwh_per_kg:.2f} kg/MWh")

    base = Path(a.baseline)
    years = sorted(int(p.stem.split("_")[1]) for p in base.glob("prices_*.parquet"))
    ref = pd.DataFrame({y: optimum(fr_spot(base / f"prices_{y}.parquet"), charge, a.kwh_per_kg, a.fixed_hours)
                        for y in years}).T

    rows = []
    for d in sorted(Path(a.mc).glob("proj_*")):
        for y in years:
            f = d / f"prices_{y}.parquet"
            if f.exists():
                rows.append({"draw": int(d.name.split("_")[1]), "year": y,
                             **optimum(fr_spot(f), charge, a.kwh_per_kg, a.fixed_hours)})
    mc = pd.DataFrame(rows)
    qs = [0.1, 0.5, 0.9]
    band = mc.groupby("year")["lcoh"].quantile(qs).unstack() if not mc.empty else pd.DataFrame()
    band_fixed = mc.groupby("year")["fixed_lcoh"].quantile(qs).unstack() if not mc.empty else pd.DataFrame()
    n_draws = mc["draw"].nunique() if not mc.empty else 0

    ref["hours_per_year"] = ref["hours"] * 8760.0 / ref["priced_hours"]
    table = ref[["lcoh", "capex_part", "power_part", "hours", "priced_hours", "hours_per_year", "load_factor",
                 "threshold", "avg_price_paid", "fixed_lcoh", "fixed_capex_part", "fixed_power_part",
                 "fixed_avg_price_paid", "baseload_lcoh"]].astype(float)
    if not band.empty:
        table = table.join(band.rename(columns={0.1: "mc_flex_p10", 0.5: "mc_flex_median", 0.9: "mc_flex_p90"}))
        table = table.join(band_fixed.rename(columns={0.1: "mc_fixed_p10", 0.5: "mc_fixed_median",
                                                      0.9: "mc_fixed_p90"}))
    table.index.name = "year"
    table.round(3).to_csv(out / "electrolyser_lcoh.csv")
    mc.round(4).to_csv(out / "electrolyser_lcoh_mc_draws.csv", index=False)
    (out / "electrolyser_lcoh_assumptions.json").write_text(json.dumps(
        {"capex_eur_per_mw": a.capex, "kwh_per_kg": a.kwh_per_kg, "wacc": a.wacc, "life_years": a.life,
         "crf": crf(a.wacc, a.life), "annual_charge_eur_per_mw": charge, "fixed_hours_per_year": a.fixed_hours,
         "prices": "FR spot, hourly", "baseline": str(base), "mc_ensemble": str(Path(a.mc)), "mc_draws": int(n_draws),
         "mc_quantiles": qs,
         "rules": {"capital_charge": "annual charge * n / 8760, n = the year's priced hours",
                   "optimum": "the k cheapest priced hours, k minimising (charge + their cost) / kg",
                   "fixed_hours": "the round(fixed_hours * n / 8760) cheapest priced hours"},
         "assumed": "perfect foresight of the year's prices; price-taker (its own demand does not move them); the "
                    "year's prices repeated over the life",
         "excluded": "O&M, stack replacement, grid tariff and taxes, water, compression, storage, transport, "
                     "ramping limits, minimum load"},
        indent=1), encoding="utf-8")
    pd.set_option("display.width", 200)
    print(table.round(2).to_string())

    fig, (ax, bx) = plt.subplots(2, 1, figsize=(13.0, 7.8), dpi=170, sharex=True,
                                 gridspec_kw={"height_ratios": [3.0, 1.3], "hspace": 0.14})
    fig.patch.set_facecolor(SURFACE)
    for a_ in (ax, bx):
        a_.set_facecolor(SURFACE)
        a_.grid(axis="y", color=GRID, lw=0.9)
        a_.set_axisbelow(True)
        for sp in ("top", "right"):
            a_.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            a_.spines[sp].set_color(AXIS)
        a_.tick_params(colors=INK2, labelsize=9.5)

    if not band.empty:
        ax.fill_between(band.index, band[0.1], band[0.9], color=BLUE_LIGHT, alpha=0.55, lw=0,
                        label=f"Optimal flexible, Monte-Carlo ({n_draws} weather draws): 10th-90th percentile",
                        zorder=1)
        ax.plot(band.index, band[0.5], color=MUTED, lw=1.2, ls=(0, (4, 2)),
                label="Optimal flexible, Monte-Carlo median", zorder=2)
    fh = f"{a.fixed_hours:,.0f}".replace(",", " ")
    ax.plot(years, ref["fixed_lcoh"], color=ORANGE, lw=2.2, ls=(0, (5, 2)), marker="s", ms=5.0, mfc=ORANGE,
            mec=SURFACE, mew=1.2, label=f"Reference scenario, running {fh} h a year (its cheapest hours)", zorder=3)
    for y in (years[0], years[-1]):
        ax.annotate(f"{ref.loc[y, 'fixed_lcoh']:.2f} €/kg", xy=(y, ref.loc[y, "fixed_lcoh"]), xytext=(0, 10),
                    textcoords="offset points", ha="center", va="bottom", fontsize=9.2, color=INK2)
    ax.plot(years, ref["lcoh"], color=BLUE, lw=2.2, marker="o", ms=5.5, mfc=BLUE, mec=SURFACE, mew=1.2,
            label="Reference scenario, optimal flexible operation", zorder=4)
    for y, dy, va in ((years[0], 10, "bottom"), (int(ref["lcoh"].astype(float).idxmin()), -12, "top"),
                      (years[-1], 10, "bottom")):
        ax.annotate(f"{ref.loc[y, 'lcoh']:.2f} €/kg", xy=(y, ref.loc[y, "lcoh"]), xytext=(0, dy),
                    textcoords="offset points", ha="center", va=va, fontsize=9.2, color=INK2)
    ax.set_title("France — theoretical cost of electrolytic hydrogen on each year's prices: optimal flexible "
                 f"operation vs {fh} h a year", fontsize=12.5, weight="600",
                 loc="left", pad=26, color=INK)
    ax.text(0, 1.02, f"Capex {a.capex / 1e6:g} M€/MW, {a.life} years at {a.wacc * 100:g}% WACC  ·  "
                     f"{a.kwh_per_kg:g} kWh/kg  ·  FR hourly spot, the year repeated over the life  ·  capex + "
                     "wholesale power only (no O&M, grid tariff, stack, compression)",
            transform=ax.transAxes, fontsize=8.4, color=INK2)
    ax.set_ylabel("€ per kg H₂", fontsize=10.5, color=INK2)
    lo = float(min(ref["lcoh"].min(), band[0.1].min() if not band.empty else np.inf))
    hi = float(max(ref["lcoh"].max(), ref["fixed_lcoh"].max(), band[0.9].max() if not band.empty else -np.inf))
    ax.set_ylim(max(0.0, lo - 0.6), hi + 0.6)
    h_, l_ = ax.get_legend_handles_labels()
    order = [i for key in ("Reference scenario, optimal", "Reference scenario, running", "Monte-Carlo")
             for i, x in enumerate(l_) if key in x]
    leg = ax.legend([h_[i] for i in order], [l_[i] for i in order], loc="upper center", frameon=False, fontsize=9.2,
                    handlelength=3.0)
    for t in leg.get_texts():
        t.set_color(INK2)

    bx.plot(years, ref["load_factor"].astype(float) * 100, color=BLUE, lw=2.0, marker="o", ms=4.5, mfc=BLUE,
            mec=SURFACE, mew=1.0, zorder=3)
    fixed_pct = min(100.0, a.fixed_hours / 8760.0 * 100)
    bx.axhline(fixed_pct, color=ORANGE, lw=2.0, ls=(0, (5, 2)), zorder=2)
    bx.annotate(f"{fh} h a year = {fixed_pct:.0f} %", xy=(years[-1], fixed_pct), xytext=(0, -6),
                textcoords="offset points", ha="right", va="top", fontsize=8.8, color=INK2)
    bx.set_ylabel("hours run\n(% of the year)", fontsize=10, color=INK2)
    bx.set_ylim(0, 100)
    bx.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f} %"))
    for y in (years[0], years[-1]):
        bx.annotate(f"{ref.loc[y, 'load_factor'] * 100:.0f} %, below {ref.loc[y, 'threshold']:.0f} €/MWh",
                    xy=(y, ref.loc[y, "load_factor"] * 100), xytext=(0, -12), textcoords="offset points",
                    ha="left" if y == years[0] else "right", va="top", fontsize=8.8, color=INK2)
    bx.set_xticks(years)
    bx.set_xticklabels([str(y) for y in years], fontsize=9.5)
    bx.set_xlim(years[0] - 0.6, years[-1] + 0.6)
    fig.subplots_adjust(left=0.075, right=0.985, top=0.9, bottom=0.07)
    p = out / "electrolyser_lcoh.png"
    fig.savefig(p, format="png", facecolor=SURFACE)
    plt.close(fig)
    print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
