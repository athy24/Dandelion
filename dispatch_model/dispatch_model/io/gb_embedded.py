"""GB embedded (distribution-connected) generation, reconstructed from GB's own energy balance.

WHY GB NEEDS THIS AND NO ENTSO-E ZONE DOES. Every other zone's load and generation come from the same TSO
submission on the same boundary, so they balance by construction. GB's two feeds do not: Elexon's FUELINST
and AGWS meter transmission-connected plant, while `demand/outturn` reports ITSDO — demand at the
TRANSMISSION boundary, already net of whatever generation sits behind it on the distribution network.
Britain has a great deal of such plant (CHP, waste-to-energy, small gas, embedded biomass), so the two
feeds are measured on different boundaries and the balance does not close. Reconciling them is a
correction, not a fit.

MEASURED, NOT ASSUMED. The reconstruction is GB's own residual:

    embedded(t) = load(t) - metered generation(t) - net interconnector import(t)

On 2024 that is 5851 MW mean (51.3 TWh) against 28297 MW of demand — a fifth of the system. Left in, it
is the reason a promoted GB ran out of plant and hit VoLL in 612 hours against 36 observed, dragging the
Netherlands to 15000 EUR/MWh with it through BritNed in 97 of them.

WHAT THE RESIDUAL IS MADE OF. Two superimposed effects, separated by measurement rather than assertion:

  * a SOLAR DOUBLE-COUNT. AGWS reports national solar, which in GB is essentially all distribution-
    connected, while ITSDO is already net of it — so solar is counted once on each side. Signature:
    residual is LOWEST at midday and collapses to 262 MW at summer noon against 6057 MW at winter noon,
    with corr(residual, solar) = -0.53. Regressing residual on (1, solar, wind, load) puts the solar
    coefficient at -1.246, and adding solar back leaves corr(residual + solar, solar) = -0.065 — i.e. the
    coefficient is -1, an exact double-count.
  * an EMBEDDED FIRM BLOCK. What remains once solar is added back is 7287 MW, flat across the day
    (6.0-9.1 GW) and across seasons (summer noon 6.6 GW vs winter noon 7.9 GW). Flat and season-blind is
    the signature of heat-led and baseload embedded plant, not of RES.

Both are corrected by the SAME subtraction, because the arithmetic collapses:

    (load + solar) - (residual + solar)  ==  load - residual

Grossing demand up to the national boundary and then removing the full embedded block is algebraically
identical to netting the raw residual off ITSDO, so the simpler form is used and is exact.

APPLIED AS A LOAD CORRECTION, NOT AS SUPPLY. Netting it off demand keeps it out of price formation, which
is right for heat-led embedded CHP (it runs on heat demand, not on price) and conservative for the rest.
The consequence to know is that GB's stack adequacy is no longer tested by the model — GB is always able
to serve its own corrected demand. That is an acceptable trade here because GB exists in this model to be
a correct NEIGHBOUR for FR/BE/NL, not to have its own adequacy assessed.

PRICING THE BLOCK INSTEAD WAS TRIED AND REJECTED ON MEASUREMENT. Netting has a real cost: with a fifth of
its demand removed GB always serves itself, prices at the bottom of its own stack, and over-prints cheap
hours 6.8x to 200x (1800 hours below +5 EUR/MWh in 2019 against 9 observed; a 2025 median of 35 against
96), exporting that cheapness through its borders. So the firm part was moved into the stack as gas across
the lower half of `EFF_RANGE["gas"]` (0.40-0.49), with demand grossed up by metered solar to keep the
boundary consistent — algebraically the same MW position, but with the firm block priced and able to be
marginal. (A must-run block at a ZERO bid would have changed nothing: in an LP, zero-priced supply and an
equal demand reduction shift the residual demand curve identically. Only a real cost changes anything.)

It failed, and worse than the defect it was aimed at:

    year   GB mean model/obs   hours at VoLL        vs netting
    2019          40 / 43            0
    2022        1107 / 233          687              14 h (2024) under netting
    2024         455 / 85           341
    NL 2024 mean error +47.4 EUR/MWh, against +23.5 under netting

The cause is the physics this docstring already stated. Priced at gas SRMC, the block costs 500+ EUR/MWh
in the 2022 gas crisis, so it withdraws exactly when the system is tight — the opposite of what heat-led
plant does. Three-year pooled log_err went 1.93 -> 1.99 on the gated zones, plus the GB/NL cascade.

WHAT WOULD ACTUALLY BE RIGHT: neither pure form. Netting is always-on and too cheap; pricing abandons the
system when tight. The block needs SPLITTING into a heat-led must-run tranche (behaving as netting does)
and a price-responsive tranche that can be marginal. That needs a composition source for Britain's
embedded fleet — a NESO/DUKES split of embedded CHP, waste and landfill gas against distribution-connected
peakers — which this project does not have. Choosing the ratio without one would be fitting.

COMPUTED PER YEAR, AND THAT IS DELIBERATE. The residual is not a stable physical constant — measured as a
share of demand it runs 17.0 % (2019), 29.7 % (2022), 25.7 % (2023), 20.7 % (2024), 8.4 % (2025). Some of
that is real (embedded gas and diesel ran hard through the 2022 crisis), but 2025's collapse is not: it
tracks AGWS wind jumping from 6.1 to 9.6 GW mean between 2024 and 2025, which is a change in what the FEED
covers rather than ten gigawatts of new turbines. So this term absorbs Elexon's evolving coverage as well
as Britain's embedded fleet, and treating it as a fixed constant would be wrong. Deriving it from each
year's own balance is what makes it robust to that: whatever the feeds report, the balance closes.

The honest reading is that this is a RECONCILIATION between two feeds, not a measurement of a physical
fleet. It is named for the dominant physical cause, and it is right about the arithmetic it corrects.

A MONTH x HOUR-OF-DAY MEDIAN, not the raw hourly residual. The raw series runs from -10555 to +22295 MW;
those tails are hours where a feed is momentarily incomplete, and subtracting 22 GW from a 28 GW demand
would import a metering glitch straight into the model as a demand collapse. The median over each
(month, hour) cell is robust to them and keeps the structure that was measured to be real. It is
deliberately the same shape of estimator as `observed_mustrun_floors`, which takes a p10 per tech-month
for the same reason.
"""
from __future__ import annotations

import os
import sqlite3

import numpy as np
import pandas as pd

#: Fewest hours a (month, hour) cell needs before its median is trusted; below it the cell falls back to
#: the whole-year median rather than to a value estimated from a handful of points.
#: Minimum hours in a (month, hour) cell before its own median is trusted; thinner cells fall back to
#: the year median. **It must be reachable.** At 40 it was not: `embedded_profile` groups ONE year into
#: 12x24 = 288 cells, so a cell holds at most 31 hours and 288 of 288 fell back — every year, silently,
#: making the delivered block a CONSTANT. The whole month-hour profile this module documents, and the
#: solar double-count it exists to remove in shape rather than only on average, was never produced:
#: measured on 2024, the true July-12h median is 671 MW against 5273 MW in January, and both were being
#: replaced by a flat 6164 MW.
#:
#: 20 keeps the guard meaningful for a PARTIAL year (a half-populated cell still falls back) while
#: admitting the 28-31 hours a complete year actually supplies.
_MIN_CELL = 20


def _balance_frame(config, year: int) -> pd.DataFrame | None:
    """Hourly load / metered generation / net import for GB, or None if any feed is missing."""
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        args = (f"{year}-01-01", f"{year + 1}-01-01")
        gen = pd.read_sql("SELECT ts_utc, sub_key, value FROM entsoe_generation WHERE series_key='GB' "
                          "AND ts_utc>=? AND ts_utc<?", con, params=args)
        load = pd.read_sql("SELECT ts_utc, value FROM entsoe_load WHERE series_key='GB' "
                           "AND ts_utc>=? AND ts_utc<?", con, params=args)
        flow = pd.read_sql("SELECT ts_utc, series_key, value FROM entsoe_flows WHERE "
                           "(series_key LIKE '%>GB' OR series_key LIKE 'GB>%') "
                           "AND ts_utc>=? AND ts_utc<?", con, params=args)
    except Exception:                                       # noqa: BLE001 — table absent → no correction
        return None
    finally:
        con.close()
    if gen.empty or load.empty:
        return None
    for d in (gen, load, flow):
        d["ts"] = pd.to_datetime(d["ts_utc"], utc=True)
    g = (gen.pivot_table(index="ts", columns="sub_key", values="value", aggfunc="mean")
            .resample("h").mean().sum(axis=1))
    ld = load.set_index("ts")["value"].resample("h").mean()
    if flow.empty:
        imp = pd.Series(0.0, index=ld.index)
    else:
        fw = flow.pivot_table(index="ts", columns="series_key", values="value",
                              aggfunc="mean").resample("h").mean()
        imp = (fw[[c for c in fw.columns if c.endswith(">GB")]].sum(axis=1)
               - fw[[c for c in fw.columns if c.startswith("GB>")]].sum(axis=1))
    return pd.DataFrame({"load": ld, "gen": g, "imp": imp}).dropna()


def embedded_profile(config, year: int) -> pd.Series | None:
    """→ {(month, hour): MW} median embedded generation for `year`, or None if GB data is missing."""
    df = _balance_frame(config, year)
    if df is None or len(df) < 1000:
        return None
    resid = df["load"] - df["gen"] - df["imp"]
    cells = resid.groupby([resid.index.month, resid.index.hour])
    prof = cells.median()
    thin = cells.size() < _MIN_CELL
    if thin.any():                                          # thin cells fall back to the year median
        prof[thin[thin].index] = float(resid.median())
    return prof.clip(lower=0.0)                             # a negative embedded block is not physical


def embedded_mw(config, year: int, index=None) -> pd.Series | None:
    """→ hourly embedded generation (MW) for GB over `index` (or the year), None if unavailable.

    Built from the (month, hour) median profile, so it is smooth and reproducible rather than carrying the
    raw residual's metering spikes.
    """
    prof = embedded_profile(config, year)
    if prof is None:
        return None
    if index is None:
        index = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h", tz="UTC")[:-1]
    idx = pd.DatetimeIndex(index)
    key = list(zip(idx.month, idx.hour))
    fallback = float(prof.median())
    return pd.Series([float(prof.get(k, fallback)) for k in key], index=idx)


#: Share of the FIRM embedded block that is HEAT-LED and therefore must-run: it cannot set a price. The
#: remainder is price-responsive and is bid into GB's stack as low-efficiency gas (embedded reciprocating
#: engines, diesel/STOR, embedded OCGT), where it CAN be marginal.
#:
#: This split is what the module docstring above names as "WHAT WOULD ACTUALLY BE RIGHT", and BOTH pure
#: forms are measurably wrong. Netting the whole block makes GB's net load equal its metered generation
#: plus imports BY CONSTRUCTION — leftover slack -15 MW mean on 2019, +77 MW on 2024 — so the zone can
#: always serve itself and prices at the bottom of its own stack: 881 modelled zero-or-negative hours
#: against 3 observed in 2019, 2491 against 238 in 2025, and a 2025 mean of 53.4 against 93.4 observed.
#: Pricing the whole block fails the other way, withdrawing the fleet exactly when the system is tight
#: (687 VoLL hours in 2022). Pricing only the MINORITY scales the withdrawal down with it, while the
#: heat-led majority still guarantees GB cannot run short.
#:
#: `DISPATCH_GB_MUSTRUN` overrides at call time; 1.0 restores the pre-split behaviour to the last MW.
#:
#: 0.75 IS A FITTED ASSUMPTION, NOT A MEASUREMENT, and the two available answers disagree — which is worth
#: recording rather than smoothing over.
#:
#: THE COMPOSITION SOURCE says 0.65. Parsing 14,552 Embedded Capacity Register records (four DNO groups,
#: 26.1 GW connected, 9.2 GW firm) against DUKES 2026 gives must-run 7.97 GW — CHP 2.2, energy-from-waste
#: 1.9, biomass 1.5, landfill gas 0.9, anaerobic digestion 0.6, sewage gas 0.27, small hydro 0.6 — against
#: price-responsive 4.30 GW (gas reciprocating engines 2.6, diesel/STOR 1.1, embedded OCGT 0.4, other 0.2).
#: Removing small hydro, which GB's `hydro_ror` already carries in `_MUSTTAKE`, gives 0.63. An independent
#: capacity-market route gives 0.80, though its own RAW basis gives 0.28 — a factor-of-three spread driven
#: entirely by which register is treated as the population. Three adversarial verifiers recommended 0.62,
#: 0.70 and 0.90. Excluding the outlier the evidence clusters 0.62-0.70.
#:
#: THE 2025 PRICE FIT says 0.75, and it says so because 0.65 BREAKS THIS MODEL. Swept against observed GB
#: 2025 (mean 93.4, median 95.7, sd 47.9, 238 hours <= 0):
#:
#:     share   mean   median   hrs<=0   VoLL   sd/obs   MAE
#:      1.00   53.4     49.6     2491      0    1.13x  44.8
#:      0.85   56.9     61.4     2253      0    1.18x  42.5
#:      0.75   59.6     70.4     2134      0    1.25x  41.5   <- MAE optimum, last arm that passes
#:      0.65   68.9     74.7     2004      4    6.81x  47.2
#:      0.55  101.6     79.2     1908     22   15.71x  76.1
#:      0.45  130.9     81.0     1819     38   20.59x 102.1
#:
#: Below 0.75 the fleet starts withdrawing exactly when the system is tight — the same failure that got the
#: fully-priced block rejected — and dispersion explodes past the markup panel's 2.0x gate. The level alone
#: would prefer ~0.55 (mean 101.6 against 93.4 observed), but it buys that level with 22 VoLL hours GB has
#: never had. 0.75 is where the level improves as far as it can WITHOUT reintroducing the pathology.
#:
#: THE DISAGREEMENT IS ITSELF A FINDING. The fleet's composition says 0.65 is right; this model cannot
#: absorb 0.65 without running short. That points at GB's dispatchable stack being thin rather than at the
#: ratio being wrong, and it is the next thing to look at. Until then GB stays too cheap — 59.6 against
#: 93.4 observed on 2025, better than the 53.4 it was, and still 36 % low.
_MUST_RUN_SHARE = 0.75

#: Efficiency assigned to the priced tranche. Embedded reciprocating engines and diesel sets are small and
#: inefficient, so the BOTTOM of the gas band is right; the band midpoint would price them as CCGTs.
_PRICED_EFF = 0.40


def must_run_share() -> float:
    """Heat-led fraction of the firm embedded block. Read at each call site so a calibration sweep can move
    it without reimporting."""
    v = os.environ.get("DISPATCH_GB_MUSTRUN")
    if v is None:
        return _MUST_RUN_SHARE
    try:
        return min(max(float(v), 0.0), 1.0)
    except ValueError:
        return _MUST_RUN_SHARE


def priced_block_mw(config, year: int) -> float:
    """→ capacity (MW) of the PRICE-RESPONSIVE tranche for `year`; 0.0 when the split is off.

    Sized on the p99 of the hourly block, not its mean: this is dispatchable CAPACITY, and a mean would let
    the LP dispatch more than the fleet can deliver in exactly the hours that decide the price."""
    share = 1.0 - must_run_share()
    if share <= 0.0:
        return 0.0
    e = embedded_mw(config, year)
    return float(np.quantile(e.to_numpy(), 0.99) * share) if e is not None else 0.0


def apply_to_netload(config, zone: str, year: int, df: pd.DataFrame) -> pd.DataFrame:
    """Net GB's embedded generation off its load, in place of a supply block. No-op for other zones.

    `df` is indexed by timestamp_utc and carries `load_mw` / `musttake_res_mw`; `netload_mw` is recomputed
    by the caller. Load is floored at zero: the correction is a subtraction of measured supply, and a
    negative demand would be an artefact of the estimator rather than a real export.
    """
    if zone != "GB" or df.empty:
        return df
    emb = embedded_mw(config, year, index=df.index)
    if emb is None:
        return df
    # ONLY THE HEAT-LED SHARE COMES OFF LOAD. The remainder is bid into GB's stack by
    # `neighbours.blocks._build_neighbour_stack`, so it can be marginal instead of silently making GB able
    # to serve itself in every hour. At `must_run_share() == 1.0` this is the previous behaviour exactly.
    mr = must_run_share()
    out = df.copy()
    out["gb_embedded_mw"] = emb.to_numpy() * mr
    out["load_mw"] = np.maximum(out["load_mw"].to_numpy() - emb.to_numpy() * mr, 0.0)
    return out
