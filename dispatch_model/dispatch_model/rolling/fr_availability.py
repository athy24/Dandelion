"""FR nuclear + reservoir availability for PROJECTION, from the step-v availability_model lake (#160).

The backtest feeds `fr_window(nuc_unavail_daily=…)` with the true REMIT nuclear outage (#78); the projection
used to pass nothing and fell back to the *reference-year* rolling-max-of-output proxy — 2030's nuclear
availability assumed to look like 2019's. This wires the availability_model's PROJECTED output instead:

  * `availability_by_tech`  [draw, day, technology, available_mw, installed_mw] → daily nuclear
    availability FRACTION (available ÷ INSTALLED). A fraction, not an absolute — so it decouples the
    *outage pattern* (maintenance schedule + forced outages the model projects) from the *capacity level*,
    which the dispatch sets from its own TYNDP trajectory (`nuc_cap`). Outage MW = (1 − frac)·nuc_cap.

    THE DENOMINATOR IS `installed_mw`, WRITTEN BY STEP V, and this is a repair. It used to be
    `available_mw.groupby(draw, year).max()` — the annual maximum of the numerator itself. The best day of
    a year is never a full-availability day, so the fraction sat ~6.8 points high: **4.3 GW of French
    nuclear that was never removed from the stack, every hour of every year**. The tell was 2027, whose
    bias was only 1.05 points against 6.5–8.0 everywhere else — `_assemble_draw` seeds every unit at full
    capacity before applying outages, so year one's maximum happens to reach true installed. The
    justification offered for the max ("captures a fleet that shrinks as reactors retire") did not hold
    either: `closure_year` was NaN for all 168 registry rows, so nothing ever retired.
  * `reservoir_budget`      [week_start, avail_energy_gwh] → the weekly FR reservoir energy budget (MWh),
    replacing the window's reference-year reservoir generation as the `hydro_reservoir` energy cap.

Both are mapped from the target year onto the reference-year window calendar by position (day-of-year /
ISO-week), the same alignment the weather shapes use. The reservoir budget is a single deterministic
series (no draw axis), so it was always applied correctly.

THE DRAW AXIS IS NOT 0..n-1, and assuming it was cost this model its entire nuclear-availability
dimension. `availability_mc/wdraw=d` is written by `project(n_draws=1, draw_ids=[d])`, so it holds ONE
series whose label is the literal `d`; `wdraw=900` holds one labelled 0; the canonical `availability`
layer holds 0 and 1. The lookup was `frac.loc[(draw % nuc_draws, year)]` — with one draw in the partition
that is `draw % 1 == 0` for every caller, so it could only ever hit a partition whose single label was 0.
Everywhere else it raised KeyError, returned None, and `fr_window` silently substituted the 2019
rolling-max proxy. **49 of the 50 Monte-Carlo draws were priced on one identical 2019 nuclear shape**, and
nothing said so — the runs printed "reusing availability_mc/wdraw=13" (true, the partition existed) and
"1 nuclear draw(s)" (true, and precisely the condition that broke the lookup). Labels are now resolved
against what the partition actually carries, and a fallback is announced rather than assumed.
"""
from __future__ import annotations

import pandas as pd


def load_fr_availability(config) -> dict | None:  # noqa: ARG001 (config kept for signature symmetry)
    """Load the step-v FR nuclear availability fraction (per draw) + weekly reservoir budget, or None.

    → {"nuc_frac": Series[(draw, year, doy) → frac], "nuc_draws": int, "source": str,
       "reservoir": {(year, iso_week): budget_mwh}}."""
    import os

    from powersim_core import lake
    # A Monte-Carlo draw writes its own availability to an isolated layer (see availability_model.project).
    # `DISPATCH_AVAIL_WDRAW` selects it, read at call time so parallel workers each see their own draw and
    # never the concatenation of everyone's. Unset -> the canonical shared tables, unchanged.
    _w = os.environ.get("DISPATCH_AVAIL_WDRAW")
    _layer, _pk = ("availability", {}) if _w is None else ("availability_mc", {"wdraw": int(_w)})
    src = _layer if _w is None else f"{_layer}/wdraw={int(_w)}"
    try:
        bt = lake.read_table(_layer, "availability_by_tech", **_pk)    # [draw, day, technology, available_mw]
    except (FileNotFoundError, KeyError, ValueError, OSError):
        return None
    nuc = bt[bt["technology"] == "nuclear"].copy()
    if nuc.empty:
        return None
    nuc["day"] = pd.to_datetime(nuc["day"])
    nuc["year"] = nuc["day"].dt.year
    nuc["doy"] = nuc["day"].dt.dayofyear
    # INSTALLED, from step v, under the same operating window that produced `available_mw` — never the
    # annual max of `available_mw` itself (see the module docstring: that cost 4.3 GW of phantom nuclear).
    if "installed_mw" in nuc.columns and nuc["installed_mw"].notna().all():
        # `.all()`, not `.any()`. A partly-written column would otherwise pass, and its NaNs meet
        # `.where(inst > 0, 1.0)` below — NaN > 0 is False, so each becomes a denominator of 1.0 and the
        # day reports 100 % availability, ZERO outage. That is the most price-distorting direction there
        # is, arrived at silently, which is the exact failure this guard exists to prevent.
        inst = nuc["installed_mw"]
    else:
        # a partition written before step v carried the column. Refuse rather than silently fall back to
        # the defective denominator: an old partition mixed into a new study is exactly the "old results
        # wearing a new label" failure this repo has already paid for twice.
        raise ValueError(
            f"step-v partition {src} has no usable `installed_mw` (written by availability_model "
            f"projection/engine.py). Re-project it; the annual-max denominator it would otherwise need "
            f"overstates nuclear availability by ~6.8 points.")
    nuc["frac"] = (nuc["available_mw"] / inst.where(inst > 0, 1.0)).clip(0.0, 1.0)
    frac = nuc.set_index(["draw", "year", "doy"])["frac"].sort_index()
    out: dict = {"nuc_frac": frac, "nuc_draws": int(nuc["draw"].nunique()),
                 "source": src, "reservoir": {}}
    try:
        rb = lake.read_table(_layer, "reservoir_budget", **_pk)        # [week_start, avail_energy_gwh]
        rb = rb.copy()
        rb["week_start"] = pd.to_datetime(rb["week_start"])
        yr = rb["week_start"].dt.year
        wk = rb["week_start"].dt.isocalendar().week.astype(int)
        out["reservoir"] = {(int(y), int(w)): float(v) * 1000.0        # GWh → MWh
                            for y, w, v in zip(yr, wk, rb["avail_energy_gwh"])}
    except (FileNotFoundError, KeyError, ValueError, OSError):
        out["reservoir"] = {}
    return out


def nuclear_unavail_daily(fr_avail: dict, target_year: int, ref_year: int, draw: int,
                          nuc_cap: float) -> dict | None:
    """{reference-year date → nuclear outage MW at `nuc_cap`} for `fr_window`, from the step-v availability
    fraction of `target_year`, for the draw RESOLVED AGAINST THE LABELS THE PARTITION ACTUALLY CARRIES.

    `draw` is an identifier, `nuc_draws` is a count, and the old code used the second to index the first:
    `frac.loc[(draw % nuc_draws, ...)]`. Every per-draw partition holds a single series labelled with its
    own draw id, so that expression asked for label 0 no matter who called, hit KeyError on all but one
    partition, and returned None into a silent 2019 proxy. Resolution is now: take the caller's label if
    the partition carries it, else wrap by POSITION among the labels present — which reproduces the old
    behaviour exactly on the canonical layer (labels 0 and 1, where count and identifier coincide) and
    stops missing everywhere else.
    """
    import os
    frac = fr_avail.get("nuc_frac")
    if frac is None or nuc_cap <= 0:
        return None
    labels = sorted(set(frac.index.get_level_values(0)))    # order-independent: the loader sorts, but this
    #                                                        function is public and takes a plain dict, so
    #                                                        it must not inherit the caller's index order
    if not labels:
        return None
    lab = int(draw) if int(draw) in labels else labels[int(draw) % len(labels)]
    try:
        yr = frac.loc[(lab, target_year)]             # Series indexed by day-of-year
    except KeyError:
        yr = None
    if yr is None or yr.empty:
        # LOUD. The whole defect was that this path was silent: `fr_window` substitutes the reference-year
        # proxy and the run looks identical to one carrying real projected availability.
        print(f"[fr_availability] WARNING {target_year}: step-v nuclear availability NOT APPLIED "
              f"(source {fr_avail.get('source', '?')}, draw {draw} → label {lab}, labels present "
              f"{labels}, years {sorted({int(y) for _, y, _ in frac.index})[:3]}…). "
              f"The window falls back to the {ref_year} rolling-max proxy.", flush=True)
        return None
    # SENSITIVITY hook: `DISPATCH_FR_NUC_AVAIL_SCALE` multiplies the availability fraction (0.99 = 1 %
    # WORSE than the projection, RELATIVE — the same reading as a "-10 % solar" sensitivity, not one
    # percentage point). It scales the fraction rather than adding outage MW so the derate follows the
    # shape: a fleet already 40 % out for maintenance in May loses less absolute MW than one at full
    # availability in January, which is how a worse forced-outage rate actually falls. Read at call time.
    scale = float(os.environ.get("DISPATCH_FR_NUC_AVAIL_SCALE", "1") or 1)
    doy_max = int(yr.index.max())
    ref_days = pd.date_range(f"{ref_year}-01-01", f"{ref_year + 1}-01-01", freq="D", tz="UTC")[:-1]
    out = {}
    for dt in ref_days:
        d = int(dt.dayofyear)
        f = yr.get(d)
        if f is None:                                   # leap-day / gap → clamp to the last available doy
            f = yr.get(min(d, doy_max), 1.0)
        out[dt.date()] = float((1.0 - min(max(float(f) * scale, 0.0), 1.0)) * nuc_cap)
    _mean = sum(out.values()) / len(out)
    print(f"[fr_availability] {target_year}: FR nuclear from step-v {fr_avail.get('source', '?')} "
          f"label {lab} — mean outage {_mean / 1000:.2f} GW of {nuc_cap / 1000:.2f} GW "
          f"(mean availability {1.0 - _mean / nuc_cap:.1%})"
          + (f", scaled x{scale}" if scale != 1.0 else ""), flush=True)
    return out


def reservoir_budget_mwh(fr_avail: dict, target_year: int, window_start) -> float | None:
    """Step-v FR reservoir energy budget (MWh) for the window whose TARGET-year start is `window_start`
    (ISO-week keyed). None where absent → the window keeps its reference-year reservoir energy."""
    res = fr_avail.get("reservoir") or {}
    if not res:
        return None
    wk = int(pd.Timestamp(window_start).isocalendar().week)
    return res.get((target_year, wk))
