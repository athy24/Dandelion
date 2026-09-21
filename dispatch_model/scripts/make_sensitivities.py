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

THE DEMAND REFERENCE IS MEASURED ON THE REFERENCE SCENARIO. The FR demand variants are percentage reductions of the
trajectory the engine actually produces, so they need that trajectory: `archive/reference_20y/fr_demand_ref.json`,
written by `scripts/run_reference_20y.py --measure-demand` (year y of draw d(y), on the ensemble's own workbook).
It is refused unless it names the current year map, its ensemble and the `--reference` workbook's bytes.

IDEMPOTENT. A rebuilt workbook is not byte-identical to the previous build (openpyxl stamps the save), and a variant's
fingerprint includes its workbook's sha256 -- so rebuilding under a set in progress would move every finished
variant to `_stale/`. `.build_stamp.json` records the reference workbook, the demand reference, the year map and
this script; when all match and every workbook still has its recorded sha256, nothing is rewritten. When they do not
(an edited comment in this script is enough), each workbook is rebuilt and then compared CELL BY CELL with the one on
disk: an identical one is kept, bytes and sha256 untouched, so only a variant whose content really changed is re-run.

    python -X utf8 -W ignore scripts/make_sensitivities.py [--out ../archive/sensitivities] [--force]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
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
# WHY THESE ARE NOT WORKBOOK EDITS, WHY THEY ARE PERCENTAGES OF THE 590 TWh REFERENCE, AND WHY THEY ARE NOT FLAT.
#
# The scenario's stated French demand lives in `dispatch_tyndp` as `demand_twh`: 540 @2030, 616 @2035,
# 680 @2040, 740 @2050. That row is read for France (`projection.zfac("FR")`) and then NEVER APPLIED,
# because with `weather_coherent: true` the French load is the step-iii projection. Editing it moves
# nothing. What the LP actually prices is the engine's own output, MEASURED on the reference scenario -- 571 TWh
# in 2046 (fr_demand_ref.json, 2026-09-17) on its way to 590 TWh in 2050, the demand moderation of commit aba6a5d
# kept as the reference by decision on 2026-09-14 (dispatch_model/DECISIONS.md).
#
# The two variants were first asked for as "demand reaching only 700 / 650 TWh instead of 740-750". Against a
# 590 TWh reference that reading is meaningless, and on 2026-09-17 the user fixed it: they are PERCENTAGE REDUCTIONS
# of the 590 TWh reference -- the proportions 700/745 and 650/745, i.e. -6.0 % and -12.8 % -- reached IN FULL IN 2050
# (554.4 and 514.8 TWh against 590), after the modelled horizon, so that 2046 carries the part of the cut the ramp
# has reached by then (-4.99 % and -10.53 %).
#
# And the cut RAMPS. A scenario that ends lower does not start lower: 2027 is all but built and the
# divergence accumulates. A flat factor would cut the first projected year as hard as the last, which is a
# different and more violent scenario, since the near years carry the tightest reserve margin.


def load_demand_ref(reference_workbook: Path) -> tuple[dict[int, float], Path]:
    """FR annual demand (TWh) of the reference scenario, MEASURED by `run_reference_20y.py --measure-demand`.

    Refused unless it was measured on the current year map (meta sha256 and year -> draw), its ensemble, and the
    very workbook the variants are copied from: a table from another reference would calibrate the demand cuts
    against a trajectory that no longer exists -- which is what the hard-coded table this replaces had become."""
    import reference_scenario as rs
    sel, meta = rs.load_map()
    f = rs.DEMAND_REF_PATH
    if not f.exists():
        raise SystemExit(f"{f} absent: run scripts/run_reference_20y.py --measure-demand")
    rec = json.loads(f.read_text(encoding="utf-8"))
    why = []
    if rec.get("map_meta_sha256") != rs.file_sha256(rs.META_PATH):
        why.append("another version of the year map meta")
    if {int(k): int(v) for k, v in rec.get("year_to_draw", {}).items()} != sel:
        why.append("another year map")
    if rec.get("model_fingerprint") != meta["model_fingerprint"]:
        why.append(f"ensemble {rec.get('model_fingerprint')}")
    if rec.get("workbook_sha256") != rs.file_sha256(reference_workbook):
        why.append(f"a workbook other than {reference_workbook.name}")
    ref = {int(y): float(v) for y, v in rec.get("fr_demand_twh", {}).items()}
    if sorted(ref) != sorted(sel):
        why.append("years other than the map's")
    if why:
        raise SystemExit(f"{f} was measured on " + ", ".join(why) + ": re-run run_reference_20y.py --measure-demand")
    return ref, f

#: the reference FR demand the reductions are percentages of, and the year it is reached (decision 2026-09-14:
#: the aba6a5d moderation, 590 TWh in 2050)
FR_DEMAND_REF_LEVEL = 590.0
FR_DEMAND_REF_YEAR = 2050
#: (variant id, reduction of the reference in FR_DEMAND_REF_YEAR). The proportions of the original request, 700/745 and
#: 650/745, read as percentages of the 590 TWh reference (user decision 2026-09-17).
DEMAND_REDUCTIONS = (("s09_fr_demand_m6", 1.0 - 700.0 / 745.0), ("s10a_fr_demand_m13", 1.0 - 650.0 / 745.0))


def demand_path(reduction: float, ref: dict[int, float], level: float = FR_DEMAND_REF_LEVEL,
                year: int = FR_DEMAND_REF_YEAR) -> tuple[str, dict, float]:
    """-> (`DISPATCH_DEMAND_SCALE` string, {year: resulting TWh}, reduction reached in the last measured year).

    The REDUCTION RAMPS LINEARLY in the year, from nothing in the first measured year to `reduction` in `year`
    (-6.04 % of 590 TWh in 2050 is 554.4 TWh): the factor in year y is 1 - reduction * (y - y0) / (year - y0). Only
    the
    measured years are written, so the dispatch never extrapolates the factor, and a projected year carries exactly
    the share of the cut the ramp has reached (19/23 of it in 2046).

    The factor is a smooth function of the year and multiplies every hour of that year alike, so each hour's weather
    sensitivity passes through untouched and the draw's weather in the reference (2036 above 2037) is neither
    smoothed nor amplified. The previous form, which held French GROWTH to a fraction of the reference's off a
    least-squares trend, answered a question stated as a TWh LEVEL; as a percentage of the reference it would bend the
    ramp wherever the trend and the 590 TWh anchor disagree (the trend reaches 603 TWh in 2050), cutting 2046 by
    -5.6 % instead of the -5.0 % a -6.0 % reduction in 2050 implies.
    """
    ys = sorted(ref)
    if not ys[-1] < year:
        raise ValueError(f"the reduction is reached in {year}, which must lie after the measured years "
                         f"{ys[0]}-{ys[-1]}")
    y0 = ys[0]
    f = {y: 1.0 - reduction * (y - y0) / (year - y0) for y in ys}
    env = "FR=" + "|".join(f"{y}:{f[y]:.4f}" for y in ys)
    return env, {y: ref[y] * f[y] for y in ys}, 1.0 - f[ys[-1]]


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


def _demand_variant(vid: str, reduction: float, ref: dict[int, float]) -> tuple:
    env, tw, cut = demand_path(reduction, ref)
    y1 = max(ref)
    lvl, yr = FR_DEMAND_REF_LEVEL, FR_DEMAND_REF_YEAR
    return (vid, f"FR demand -{reduction:.1%} vs the {lvl:.0f} TWh reference in {yr} (-{cut:.1%} in {y1})", None,
            {"DISPATCH_DEMAND_SCALE": env},
            f"A {reduction:.1%} reduction of the {lvl:.0f} TWh reference, reached in full in {yr} "
            f"({(1 - reduction) * lvl:.0f} TWh). The reduction ramps linearly from nothing in {min(ref)} and reaches "
            f"-{cut:.1%} in {y1}, the last modelled year ({tw[y1]:.0f} TWh against {ref[y1]:.0f}). See `demand_path`.")


def variants(demand_ref: dict[int, float]):
    """[(id, label, workbook_edits | None, env, rationale)] — the 19 sensitivities and the baseline."""
    return [
        ("s00_baseline", "Baseline on the REFERENCE scenario", None, {},
         "The comparison point. Same code, same reference scenario (weather, availability and FR frames of the "
         "ensemble draws the year map names), REFERENCE_SCENARIOS unchanged."),

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

        *(_demand_variant(vid, red, demand_ref) for vid, red in DEMAND_REDUCTIONS),

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


def _retry(fn, attempts: int = 6):
    """Retry a file operation OneDrive may briefly block (it picks up a freshly written file to sync it)."""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))


def same_content(a: Path, b: Path) -> bool:
    """Two workbooks hold the same sheets, in the same order, with the same cell values and formulas."""
    wa, wb = load_workbook(a, read_only=True), load_workbook(b, read_only=True)
    try:
        if wa.sheetnames != wb.sheetnames:
            return False
        return all(list(wa[n].iter_rows(values_only=True)) == list(wb[n].iter_rows(values_only=True))
                   for n in wa.sheetnames)
    finally:
        wa.close()
        wb.close()


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", default=str(ROOT / "scenarios.xlsx"))
    ap.add_argument("--out", default=str(ROOT / "archive" / "sensitivities"))
    ap.add_argument("--force", action="store_true", help="rebuild even if the stamp says the set is current")
    a = ap.parse_args()
    ref, out = Path(a.reference), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"reference: {ref}  ({ref.stat().st_size/1e6:.2f} MB)")
    demand_ref, demand_f = load_demand_ref(ref)

    import reference_scenario as rs
    stamp = {"reference_sha256": _sha(ref), "demand_ref_sha256": _sha(demand_f),
             "map_meta_sha256": rs.file_sha256(rs.META_PATH), "script_sha256": _sha(Path(__file__))}
    stamp_f, mp = out / ".build_stamp.json", out / "manifest.json"
    if not a.force and stamp_f.exists() and mp.exists():
        was = json.loads(stamp_f.read_text(encoding="utf-8"))
        man = json.loads(mp.read_text(encoding="utf-8"))
        if (was.get("inputs") == stamp and all(Path(v["workbook"]).exists()
                                               and _sha(Path(v["workbook"])) == v.get("workbook_sha256")
                                               for v in man)):
            print(f"{len(man)} variants in {out} are current (.build_stamp.json) - nothing rebuilt")
            return 0
    stamp_f.unlink(missing_ok=True)                        # invalid until the new set is complete

    manifest = []
    for vid, label, edit, env, why in variants(demand_ref):
        rec = {"id": vid, "label": label, "env": dict(env), "rationale": why, "changes": []}
        # EVERY variant gets its own frozen copy, the baseline included. The set is only comparable if all
        # nineteen read a workbook that cannot move under them: `scenarios.xlsx` stays hand-editable, and a
        # later edit there must not silently redefine what the baseline was.
        wbp = out / f"{vid}.xlsx"
        tmp = out / f".{vid}.tmp.xlsx"                     # openpyxl reads by extension
        shutil.copyfile(ref, tmp)
        if edit is not None:
            wb = load_workbook(tmp)
            rec["changes"] = edit(wb)
            wb.save(tmp)
        if wbp.exists() and same_content(tmp, wbp):
            _retry(tmp.unlink)                             # identical cells: keep the file, and its sha256
        else:
            _retry(lambda src=tmp, dst=wbp: os.replace(src, dst))
        rec["workbook"] = str(wbp)
        rec["workbook_sha256"] = _sha(wbp)
        manifest.append(rec)
        print(f"\n{vid:24s} {label}")
        for c in rec["changes"][:4]:
            print(f"    {c}")
        if env:
            print(f"    env {env}")

    print("")
    print("FR demand paths (measured reference -> variant, TWh):")
    paths = {vid: demand_path(red, demand_ref) for vid, red in DEMAND_REDUCTIONS}
    print(f"{'year':6s} {'reference':>10s} " + " ".join(f"{vid:>20s}" for vid in paths))
    for y in sorted(demand_ref):
        print(f"{y:<6d} {demand_ref[y]:10.1f} " + " ".join(f"{paths[v][1][y]:20.1f}" for v in paths))
    for (vid, red), (_e, tw, cut) in zip(DEMAND_REDUCTIONS, paths.values(), strict=True):
        y1 = max(tw)
        print(f"  {vid}: -{red:.2%} of {FR_DEMAND_REF_LEVEL:.0f} TWh in {FR_DEMAND_REF_YEAR} "
              f"({(1 - red) * FR_DEMAND_REF_LEVEL:.1f} TWh); {y1} {tw[y1]:.1f} TWh (-{cut:.2%}); horizon energy "
              f"{sum(tw.values())/sum(demand_ref.values()) - 1:+.2%}")

    tmp = mp.with_name(mp.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    os.replace(tmp, mp)
    stamp_f.write_text(json.dumps({"inputs": stamp, "built_utc": datetime.now(UTC).isoformat(timespec="seconds")},
                                  indent=1), encoding="utf-8")
    print(f"\n{len(manifest)} variants -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
