"""Build the sensitivity variants: one workbook per variant, plus the manifest the runner executes.

A sensitivity is ONE changed assumption against an otherwise identical reference. So each variant starts as
a byte copy of REFERENCE_SCENARIOS, and only the cells this variant is about are rewritten — `openpyxl` on
the copy, never a re-export, so no tab the variant does not name can drift. What cannot be expressed as a
cell (France's demand level, its nuclear availability) is carried by an environment variable instead, and
the manifest records which, so a reader can always see the whole of what moved.

WHERE THE LINE FALLS, and why. `dispatch_tyndp` demand_twh is read for France and then NOT applied — with
`weather_coherent` on, FR load is the step-iii projection. Editing that cell would move nothing while
looking authoritative. Availability is the same: it comes from the step-v lake, not from a cell. Everything
else here IS a cell, and is edited as one.

TWO CONVENTIONS, stated because they are choices and not deductions:

  * COMMODITIES move their LONG-RUN anchors only (years >= 2031). 2027-2030 are quoted forwards; a
    sensitivity on the long-run carbon or gas price should not overwrite the market's own near term. So
    "CO2 175" means the 197.5 EUR/t plateau becomes 175, with the interpolation from 2030 following.
  * RES capacity moves the PROJECTED years only (>= 2027). 2025-2026 are built or building and sit outside
    the horizon. Inside it the fleet is uniformly -10 % / -20 % / +5 % against the reference, which is what
    the sensitivity names.

    python -X utf8 -W ignore scripts/make_sensitivities.py [--out ../archive/sensitivities]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]                 # …/PriceModeling/dispatch_model
ROOT = REPO.parent

#: commodity years treated as long-run (the near term is a quoted forward curve — see the module docstring)
LONGRUN_FROM = 2031
#: first projected year; RES capacity edits start here
PROJ_FROM = 2027


# ---- cell-level editors ------------------------------------------------------------------------------
def _rows(ws):
    hdr = {c.value: i + 1 for i, c in enumerate(ws[1])}
    return hdr, range(2, ws.max_row + 1)


def set_commodity(wb, commodity: str, level: float) -> list[str]:
    """Every `dispatch_commodities` row of `commodity` from LONGRUN_FROM on takes `level`."""
    ws, out = wb["dispatch_commodities"], []
    hdr, rng = _rows(ws)
    for r in rng:
        if str(ws.cell(r, hdr["commodity"]).value).strip().lower() != commodity:
            continue
        y = ws.cell(r, hdr["year"]).value
        if y is None or int(y) < LONGRUN_FROM:
            continue
        was = ws.cell(r, hdr["price"]).value
        ws.cell(r, hdr["price"]).value = level
        ws.cell(r, hdr["source"]).value = f"sensitivity: long-run {commodity} = {level}"
        out.append(f"{commodity} {int(y)}: {was} -> {level}")
    return out


def scale_res_capacity(wb, techs: tuple[str, ...], factor: float, from_year: int = PROJ_FROM) -> list[str]:
    """Scale FR `res_capacity_trajectories` capacity for `techs` from `from_year` on."""
    ws, out = wb["res_capacity_trajectories"], []
    hdr, rng = _rows(ws)
    tot = {}
    for r in rng:
        t = str(ws.cell(r, hdr["technology"]).value)
        y = ws.cell(r, hdr["year"]).value
        if t not in techs or y is None or int(y) < from_year:
            continue
        c = ws.cell(r, hdr["capacity_mw"])
        before = float(c.value or 0.0)
        c.value = round(before * factor, 1)
        tot.setdefault(int(y), [0.0, 0.0])
        tot[int(y)][0] += before
        tot[int(y)][1] += c.value
    for y in (from_year, 2035, 2046):
        if y in tot:
            out.append(f"{'+'.join(techs)} {y}: {tot[y][0]:.0f} -> {tot[y][1]:.0f} MW")
    return out


def scale_tyndp(wb, variable: str, factor: float, zones=None, exclude=("FR",)) -> list[str]:
    """Scale a `dispatch_tyndp` variable for the selected zones, leaving the 2019 reference row alone.

    The projection reads target/REFERENCE ratios, so scaling 2019 too would cancel exactly and the
    sensitivity would be a no-op — the failure mode this note exists to prevent.
    """
    ws, out = wb["dispatch_tyndp"], []
    hdr, rng = _rows(ws)
    touched = {}
    for r in rng:
        if str(ws.cell(r, hdr["variable"]).value) != variable:
            continue
        z, y = str(ws.cell(r, hdr["zone"]).value), ws.cell(r, hdr["year"]).value
        if y is None or int(y) <= 2019:
            continue
        if (zones is not None and z not in zones) or z in (exclude or ()):
            continue
        c = ws.cell(r, hdr["value"])
        before = float(c.value or 0.0)
        c.value = round(before * factor, 3)
        touched.setdefault(z, []).append(int(y))
    for z in sorted(touched):
        out.append(f"{variable} {z} x{factor} over {min(touched[z])}-{max(touched[z])}")
    return out


def replace_tyndp_nuclear(wb, zone: str, series: dict[int, float], note: str) -> list[str]:
    """Replace `cap_nuclear_gw` for one zone with `series` (year -> GW), adding rows as needed."""
    ws = wb["dispatch_tyndp"]
    hdr, rng = _rows(ws)
    seen, drop = {}, []
    for r in rng:
        if (str(ws.cell(r, hdr["variable"]).value) == "cap_nuclear_gw"
                and str(ws.cell(r, hdr["zone"]).value) == zone):
            y = int(ws.cell(r, hdr["year"]).value)
            if y in series:
                ws.cell(r, hdr["value"]).value = round(series[y], 3)
                seen[y] = True
            else:
                drop.append(r)                             # a stale anchor would interpolate against us
    for r in reversed(drop):
        ws.delete_rows(r)
    for y, v in sorted(series.items()):
        if y in seen:
            continue
        r = ws.max_row + 1
        ws.cell(r, hdr["zone"]).value = zone
        ws.cell(r, hdr["variable"]).value = "cap_nuclear_gw"
        ws.cell(r, hdr["year"]).value = int(y)
        ws.cell(r, hdr["value"]).value = round(v, 3)
    return [note, "  " + "  ".join(f"{y}:{series[y]:.2f}" for y in sorted(series))]


def freeze_large_load(wb, variable: str, level: float, from_year: int) -> list[str]:
    """Hold one `demand_new_large_loads` variable at `level` for every year >= `from_year`.

    Used for the electrolysis capacity cap. It freezes the CAPACITY only: `electrolysis_load_factor` still
    rises on its own trajectory (0.45 in 2030 to 0.60 in 2046), because a stalled build-out says nothing
    about how hard the units that were built are run. So the frozen fleet still consumes more energy each
    year, just from 2 GW instead of a growing base -- which is what "stuck at 2 GW capacity" means and not
    what "stuck at 2 GW of load" would mean.
    """
    ws, out = wb["demand_new_large_loads"], []
    hdr, rng = _rows(ws)
    for r in rng:
        if str(ws.cell(r, hdr["variable"]).value) != variable:
            continue
        y = ws.cell(r, hdr["year"]).value
        if y is None or int(y) < from_year:
            continue
        c = ws.cell(r, hdr["value"])
        before = float(c.value or 0.0)
        c.value = level
        if int(y) in (from_year, 2035, 2046):
            out.append(f"{variable} {int(y)}: {before:.3f} -> {level:.3f} GW")
    return out


def add_ntc(wb, border: str, project: str, mw: float, year: int) -> list[str]:
    ws = wb["dispatch_ntc_newbuild"]
    hdr, _ = _rows(ws)
    r = ws.max_row + 1
    for col, val in (("border", border), ("project", project), ("capacity_mw", mw),
                     ("commissioning_year", year), ("scenario", "reference"),
                     ("source", "sensitivity: additional FR-ES capacity")):
        if col in hdr:
            ws.cell(r, hdr[col]).value = val
    return [f"{border} +{mw:.0f} MW in {year} (scenario tier 'reference', so it is loaded by default)"]


# ---- FR demand level paths ----------------------------------------------------------------------------
#
# WHY THESE ARE NOT WORKBOOK EDITS, AND WHY THEY ARE NOT FLAT.
#
# The scenario's stated French demand lives in `dispatch_tyndp` as `demand_twh`: 540 @2030, 616 @2035,
# 680 @2040, 740 @2050. That row is read for France (`projection.zfac("FR")`) and then NEVER APPLIED,
# because with `weather_coherent: true` the French load is the step-iii projection. Editing it moves
# nothing. What the LP actually prices is the engine's own output, MEASURED below -- and that lands at
# 556 TWh in 2046, some 160 TWh under the scenario the workbook states. The gap is a separate defect,
# under its own investigation; it is recorded here because it is the reason these variants are expressed
# as PROPORTIONS rather than as absolute TWh targets.
#
# So "demand reaching only 700 TWh instead of 740-750" is built as the proportional cut those numbers
# represent -- 700/745 and 650/745 -- applied to the trajectory the engine really produces. The headline
# levels are therefore 523 and 485 TWh in 2046, not 700 and 650, and the manifest says so.
#
# And the cut RAMPS. A scenario that ends lower does not start lower: 2027 is all but built and the
# divergence accumulates. A flat factor would cut the first projected year as hard as the last, which is a
# different and more violent scenario, since the near years carry the tightest reserve margin.

#: FR annual demand (TWh) as the engine projects it on REFERENCE_WEATHER_20Y at realization 0 under
#: REFERENCE_SCENARIOS. MEASURED, not asserted: `scratchpad/fr_demand_traj.py` re-derives it in ~4 minutes
#: and MUST be re-run if the cube, the `demand_*` tabs or the demand engine change -- a stale table here
#: would leave the variants calibrated against a trajectory that no longer exists.
FR_DEMAND_REF_TWH = {
    2027: 449.3, 2028: 464.5, 2029: 477.2, 2030: 476.9, 2031: 481.4, 2032: 487.6, 2033: 491.8,
    2034: 503.3, 2035: 511.2, 2036: 522.9, 2037: 517.3, 2038: 525.9, 2039: 530.1, 2040: 543.0,
    2041: 538.5, 2042: 539.9, 2043: 553.5, 2044: 562.8, 2045: 572.2, 2046: 556.3,
}

#: the end-of-period level the request was framed against ("instead of 740-750"), i.e. the workbook's own
#: stated scenario. The variants are cuts RELATIVE to this, transposed onto the measured path.
FR_DEMAND_NOMINAL_2046 = 745.0


def demand_path(target_twh: float, ref: dict | None = None,
                nominal: float = FR_DEMAND_NOMINAL_2046) -> tuple[str, dict, float]:
    """-> (`DISPATCH_DEMAND_SCALE` string, {year: resulting TWh}, growth kept as a fraction of reference).

    The ramp is taken off a least-squares TREND through the measured trajectory, not off the trajectory
    itself. The measured series carries the draw's weather -- 2036 sits above 2037 -- and a factor derived
    from it would be jagged and, worse, would scale the weather variation along with the level, so a
    lower-demand France would also have a flatter response to a cold winter. Off the trend, the factor is
    smooth in the year and every hour's weather sensitivity passes through untouched.
    """
    ref = ref or FR_DEMAND_REF_TWH
    ys = sorted(ref)
    n = len(ys)
    sx, sy = sum(ys), sum(ref[y] for y in ys)
    sxx, sxy = sum(y * y for y in ys), sum(y * ref[y] for y in ys)
    b = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    a = (sy - b * sx) / n
    trend = {y: a + b * y for y in ys}
    t0, t1 = trend[ys[0]], trend[ys[-1]]
    ratio = target_twh / nominal
    k = (ratio * t1 - t0) / (t1 - t0)                  # growth kept, as a fraction of the reference's
    f = {y: (t0 + k * (trend[y] - t0)) / trend[y] for y in ys}
    env = "FR=" + "|".join(f"{y}:{f[y]:.4f}" for y in ys)
    return env, {y: ref[y] * f[y] for y in ys}, k


# ---- nuclear lifetime rules --------------------------------------------------------------------------
def fr_flat_lifetime(years: tuple[int, ...], life: int = 60) -> dict[int, float]:
    """FR `cap_nuclear_gw` under a STRICT `life`-year rule — no glide, no extension beyond it.

    The reference instead glides to ~52 GW in 2050 by retiring the oldest at 60 y and EXTENDING the rest
    (`nuclear_fleet.glide_closures`). This is the other side of that judgement: nothing is extended.
    """
    from dispatch_model.config import load_config
    from dispatch_model.stacks import nuclear_fleet as nf
    from dispatch_model.stacks.fr_stack import build_fr_stack
    cfg = load_config(str(REPO / "config.yaml"))
    wb_path = cfg.resolve(cfg.section("assumptions")["workbook"])
    stack = build_fr_stack(cfg)
    stack = stack[stack["tech"] == "nuclear"][["name", "capacity_mw"]]
    reg = pd.read_excel(wb_path, sheet_name="avail_fleet_registry")
    reg = reg[reg["technology"] == "nuclear"][["name", "commissioning_year"]]
    fr = stack.merge(reg, on="name", how="left")
    units = []
    for r in fr.itertuples():
        com, closed = nf.FR_MISSING.get(str(r.name), (None, None))
        if r.commissioning_year == r.commissioning_year:
            com = int(r.commissioning_year)
        if com is not None:
            units.append((float(r.capacity_mw), int(com), closed))
    nb = pd.read_excel(wb_path, sheet_name="dispatch_nuclear_newbuild")
    newbuild = [(str(r.zone), float(r.capacity_mw), int(r.commissioning_year)) for r in nb.itertuples()]
    old_life = nf.LIFETIME_YEARS
    nf.LIFETIME_YEARS = int(life)
    try:
        return {y: (nf.capacity_mw("FR", y, units) + nf.newbuild_mw("FR", y, newbuild)) / 1e3
                for y in years}
    finally:
        nf.LIFETIME_YEARS = old_life


def es_lifetime(years: tuple[int, ...], life: int = 60) -> dict[int, float]:
    """ES `cap_nuclear_gw` if every Spanish reactor ran to `life` years instead of the 2019 protocol.

    Spain sits in `PHASE_OUT_ZONES`, so its reference closures are the protocol calendar (2027-2035) and
    the age rule never applies. Here the calendar is dropped and each unit runs `life` years from first
    grid connection: Almaraz 1/2 1983/1984, Asco 1/2 1984/1986, Cofrentes 1985, Vandellos 2 and Trillo
    1988 — so 2043, 2044, 2044, 2046, 2045, 2048, 2048.
    """
    from dispatch_model.stacks import nuclear_fleet as nf
    es = [(mw, com) for z, _n, mw, com, _c in nf._REACTORS if z == "ES"]
    return {y: sum(mw for mw, com in es if com + life > y) / 1e3 for y in years}


# ---- the variants ------------------------------------------------------------------------------------
#: anchor years written for a replaced nuclear trajectory. Every closure year is an anchor: the sheet is
#: linearly interpolated, so a fleet that shuts in 2043 and is only anchored at 2040/2045 bleeds away
#: smoothly from 2040 instead of stepping — the same defect that once read German nuclear at 1.4 GW in 2024.
NUC_YEARS = tuple(sorted({2019, 2023, 2025, 2028, 2030, 2032, 2035, *range(2036, 2051)}))


def variants():
    """[(id, label, workbook_edits | None, env, rationale)] — the 18 sensitivities and the baseline."""
    return [
        ("s00_baseline", "Baseline on REFERENCE_WEATHER_20Y", None, {},
         "The comparison point. Same code, same weather, REFERENCE_SCENARIOS unchanged."),

        ("s01_co2_175", "CO2 175 EUR/t long-run",
         lambda wb: set_commodity(wb, "co2", 175.0), {},
         "Long-run carbon plateau 197.5 -> 175; quoted 2027-2030 forwards unchanged."),
        ("s02_co2_150", "CO2 150 EUR/t long-run",
         lambda wb: set_commodity(wb, "co2", 150.0), {},
         "Long-run carbon plateau 197.5 -> 150."),

        ("s03_gas_30", "Gas 30 EUR/MWh long-run",
         lambda wb: set_commodity(wb, "gas", 30.0), {},
         "Long-run TTF ~36 -> 30; quoted 2027-2029 forwards unchanged."),
        ("s04_gas_25", "Gas 25 EUR/MWh long-run",
         lambda wb: set_commodity(wb, "gas", 25.0), {},
         "Long-run TTF ~36 -> 25."),

        ("s05_fr_solar_m10", "FR solar -10 %",
         lambda wb: scale_res_capacity(wb, ("pv_utility", "pv_distributed", "pv_btm"), 0.90), {},
         "All three French PV segments 10 % below the reference build-out, every projected year."),
        ("s06_fr_solar_m20", "FR solar -20 %",
         lambda wb: scale_res_capacity(wb, ("pv_utility", "pv_distributed", "pv_btm"), 0.80), {},
         "As above at 20 %."),

        ("s07_fr_wind_m10", "FR onshore wind -10 %",
         lambda wb: scale_res_capacity(wb, ("wind_onshore",), 0.90), {},
         "French onshore fleet 10 % below the reference build-out. Offshore untouched."),
        ("s08_fr_wind_m20", "FR onshore wind -20 %",
         lambda wb: scale_res_capacity(wb, ("wind_onshore",), 0.80), {},
         "As above at 20 %."),

        ("s09_fr_demand_700eq", "FR demand: the 700 TWh cut (-6.0 % by 2046)", None,
         {"DISPATCH_DEMAND_SCALE": demand_path(700.0)[0]},
         "A shallower electrification path: French growth kept at 69 % of the reference's, ramping from "
         "no change in 2027 to -6.0 % in 2046 (523 TWh against 556). This is the proportional cut that "
         "700 TWh represents against the 745 the scenario states -- NOT 700 TWh in absolute terms, which "
         "against the engine's own 556 would be a 26 % increase. See `demand_path`."),
        ("s10a_fr_demand_650eq", "FR demand: the 650 TWh cut (-12.8 % by 2046)", None,
         {"DISPATCH_DEMAND_SCALE": demand_path(650.0)[0]},
         "As above with growth kept at 34 % of the reference's: -12.8 % by 2046 (485 TWh against 556)."),

        ("s10b_fr_h2_2gw", "FR electrolysis frozen at 2 GW from 2030",
         lambda wb: freeze_large_load(wb, "electrolysis_capacity", 2.0, 2030), {},
         "Households, transport and industry follow the reference exactly; only the hydrogen / RFNBO / "
         "e-fuel build-out stalls, at 2 GW of electrolysers from 2030 instead of 2.3 rising to 4.8. The "
         "load factor keeps its own trajectory, so the frozen fleet still runs harder each year."),

        ("s11_fr_nuc_avail_m1", "FR nuclear availability -1 %", None,
         {"DISPATCH_FR_NUC_AVAIL_SCALE": "0.99"},
         "Step-v availability fraction x0.99 (RELATIVE, not one point). Capacity unchanged."),
        ("s12_fr_nuc_avail_m5", "FR nuclear availability -5 %", None,
         {"DISPATCH_FR_NUC_AVAIL_SCALE": "0.95"},
         "As above at x0.95."),

        ("s13_fr_retire_60", "FR nuclear retired at 60 years",
         lambda wb: replace_tyndp_nuclear(wb, "FR", fr_flat_lifetime(NUC_YEARS, 60),
                                          "FR cap_nuclear_gw on a strict 60-year rule (no extension)"),
         {}, "The reference extends the younger fleet to glide to ~52 GW in 2050. Here nothing is "
             "extended: every reactor stops at 60 years. EPR2 new build is unchanged."),

        ("s14_fr_wind_repower_p5", "FR onshore wind +5 % from 2030 (repowering)",
         lambda wb: scale_res_capacity(wb, ("wind_onshore",), 1.05, from_year=2030), {},
         "Repowering raises site capacity 5 % from 2030. The cohort CF uplift in "
         "`res_technology_vintages` is untouched, so this is the capacity leg only."),

        ("s15_nonfr_solar_p10", "Non-FR solar +10 %",
         lambda wb: scale_tyndp(wb, "cap_solar_gw", 1.10), {},
         "Every neighbour zone's solar trajectory +10 % (2019 reference row untouched)."),
        ("s16_nonfr_wind_p10", "Non-FR wind +10 %",
         lambda wb: scale_tyndp(wb, "cap_wind_gw", 1.10), {},
         "Every neighbour zone's wind trajectory +10 %. The neighbour model carries ONE wind aggregate "
         "(`cap_wind_gw`), so this is all wind, not onshore alone."),

        ("s17_fres_ntc_p2000", "FR-ES interconnection +2000 MW in 2035",
         lambda wb: add_ntc(wb, "FR-ES", "Sensitivity: FR-ES reinforcement", 2000.0, 2035), {},
         "On top of the Bay of Biscay link (2200 MW, 2028), taking FR-ES to ~7000 MW from 2035."),

        ("s18_es_nuc_60", "Spanish nuclear to 60 years",
         lambda wb: replace_tyndp_nuclear(wb, "ES", es_lifetime(NUC_YEARS, 60),
                                          "ES cap_nuclear_gw on a 60-year rule (protocol dropped)"),
         {}, "The 2019 protocol retires Spain's fleet 2027-2035. Here each unit runs 60 years, so 7.4 GW "
             "survives to 2043 and 2.2 GW is still open in 2046."),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", default=str(ROOT / "scenarios.xlsx"))
    ap.add_argument("--out", default=str(ROOT / "archive" / "sensitivities"))
    a = ap.parse_args()
    ref, out = Path(a.reference), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"reference: {ref}  ({ref.stat().st_size/1e6:.2f} MB)")

    manifest = []
    for vid, label, edit, env, why in variants():
        rec = {"id": vid, "label": label, "env": dict(env), "rationale": why, "changes": []}
        # EVERY variant gets its own frozen copy, the baseline included. The set is only comparable if all
        # nineteen read a workbook that cannot move under them: `scenarios.xlsx` stays hand-editable, and a
        # later edit there must not silently redefine what the baseline was.
        wbp = out / f"{vid}.xlsx"
        shutil.copyfile(ref, wbp)
        if edit is not None:
            wb = load_workbook(wbp)
            rec["changes"] = edit(wb)
            wb.save(wbp)
        rec["workbook"] = str(wbp)
        manifest.append(rec)
        print(f"\n{vid:24s} {label}")
        for c in rec["changes"][:4]:
            print(f"    {c}")
        if env:
            print(f"    env {env}")

    print("")
    print("FR demand paths (measured reference -> variant, TWh):")
    print(f"{'year':6s} {'reference':>10s} " + " ".join(f"{t:>10.0f}" for t in (700.0, 650.0)))
    paths = {t: demand_path(t) for t in (700.0, 650.0)}
    for y in sorted(FR_DEMAND_REF_TWH):
        print(f"{y:<6d} {FR_DEMAND_REF_TWH[y]:10.1f} "
              + " ".join(f"{paths[t][1][y]:10.1f}" for t in (700.0, 650.0)))
    for t, (_e, tw, k) in paths.items():
        print(f"  {t:.0f}-equivalent: growth kept at {k:.1%} of reference, "
              f"2046 {tw[2046]:.1f} TWh, horizon energy "
              f"{sum(tw.values())/sum(FR_DEMAND_REF_TWH.values()) - 1:+.2%}")

    mp = out / "manifest.json"
    mp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"\n{len(manifest)} variants -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
