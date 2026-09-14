"""Make step v model the SAME French nuclear fleet the dispatch prices — retirements and new build.

THE MISMATCH. Step v projected 59 reactors that never retire and never gain a sibling: `installed_mw` was
64,009 MW flat across 2027-2046. The dispatch meanwhile scales `cap_nuclear_gw` down to 56.5 GW by 2045 and
folds in ~10 GW of EPR2 commissioning 2038-2046. The availability FRACTION step v produces was therefore
computed on one fleet and applied to another, and the repair that fixed the denominator made it worse: the
fraction moved 0.823 -> 0.744, so the absolute outage MW carried onto the dispatch's fleet grew about 45 %.
By 2044 an outage rate earned by 59 ageing reactors was being applied to a fleet that had retired its
oldest units and commissioned six new ones.

WHAT THIS WRITES into `avail_fleet_registry`:

  * CLOSURES for the existing fleet, from `dispatch_model.stacks.nuclear_fleet.glide_closures` — the same
    rule that produced the dispatch's own `cap_nuclear_gw` trajectory, so the two fleets retire together
    instead of by coincidence.
  * THE EPR2 UNITS from `dispatch_nuclear_newbuild`, with their commissioning years, so they enter step v
    when they enter the dispatch. `_operating_mask` already zeroes a unit before its commissioning year, so
    they contribute nothing until they exist.

AND THE NEW UNITS GET THEIR OWN OUTAGE RATE, which is the point. Two mechanisms, both physical rather than
asserted:

  * PLANNED: palier `EPR`, the class Flamanville 3 already carries, calibrated at an 18.0-month refuelling
    cycle against 12.0-17.0 for the legacy paliers. Fewer outages per decade, and the seasonal placement
    and duration distributions of an EPR rather than a CPY. EPR2 is a simplified EPR; using the EPR class
    is an ASSUMPTION, and a less invented one than fabricating a class with no calibration behind it.
  * FORCED: `projection/forced.py` now accrues age creep from each unit's OWN commissioning year against
    the age the calibrated fleet had at the calibration midpoint, instead of from the calendar. A 2038 unit
    carried ~25 years of creep by 2046 under the old rule; it now carries none, while a 1980 reactor still
    ages exactly as before. That single change is most of what "a distinct rate for the new units" means.

    python -X utf8 scripts/sync_nuclear_fleet.py [--write]
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(_ROOT / "dispatch_model"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from availability_model.config import load_config  # noqa: E402

SHEET = "avail_fleet_registry"
#: palier assigned to EPR2 — see the module docstring; the class is Flamanville 3's, calibrated at 18 months
EPR2_PALIER = "EPR"


def main() -> int:
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.yaml"))
    from powersim_core.scenario import resolve as resolve_wb
    wb_path = resolve_wb(cfg.resolve(cfg.section("assumptions")["workbook"]))
    y0 = int(cfg.section("projection")["horizon"]["start_year"])
    y1 = int(cfg.section("projection")["horizon"]["end_year"])

    reg = pd.read_excel(wb_path, sheet_name=SHEET)
    nuc = reg[reg.technology == "nuclear"]
    print(f"registry nuclear before: {len(nuc)} units, {nuc.capacity_mw.sum() / 1e3:.2f} GW, "
          f"closures {int(nuc.closure_year.notna().sum())}")

    # --- the dispatch's own retirement rule, applied to the dispatch's own capacities -------------------
    from dispatch_model.config import load_config as d_load
    from dispatch_model.stacks import nuclear_fleet as nf
    from dispatch_model.stacks.fr_stack import build_fr_stack
    dcfg = d_load(str(_ROOT / "dispatch_model" / "config.yaml"))
    stack = build_fr_stack(dcfg)
    stack = stack[stack.tech == "nuclear"][["name", "capacity_mw"]]

    nb = pd.read_excel(wb_path, sheet_name="dispatch_nuclear_newbuild")
    newbuild = [(str(r.zone), float(r.capacity_mw), int(r.commissioning_year)) for r in nb.itertuples()]

    units, by_name = [], {}
    for r in nuc.itertuples():
        com, closed = nf.FR_MISSING.get(str(r.name), (None, None))
        if pd.notna(r.commissioning_year):
            com = int(r.commissioning_year)
        if pd.notna(r.closure_year):
            closed = int(r.closure_year)
        if com is None:
            continue
        mw = float(stack.loc[stack.name == r.name, "capacity_mw"].iloc[0]) if (stack.name == r.name).any() \
            else float(r.capacity_mw)
        units.append((mw, com, closed))
        by_name[(mw, com, closed)] = str(r.name)
    glided = nf.glide_closures(units, newbuild)
    close_of = {}
    for (mw, com, closed) in glided:
        nm = by_name.get((mw, com, closed if closed is not None else None))
        # glide_closures fills the `closed` slot, so match on the (mw, com) pair it preserved
        if nm is None:
            for (m2, c2, _x2), n2 in by_name.items():
                if abs(m2 - mw) < 1e-6 and c2 == com:
                    nm = n2
                    break
        if nm is not None and closed is not None and closed <= y1:
            close_of[nm] = int(closed)

    print(f"\nretirements inside the horizon ({y0}-{y1}): {len(close_of)}")
    for nm, y in sorted(close_of.items(), key=lambda kv: kv[1])[:8]:
        print(f"    {nm:16s} {y}")
    if len(close_of) > 8:
        print(f"    … and {len(close_of) - 8} more")

    fr_nb = [(u, mw, yr) for (z, mw, yr) in newbuild if z == "FR"
             for u in [next((str(r.unit) for r in nb.itertuples()
                             if int(r.commissioning_year) == yr and float(r.capacity_mw) == mw), f"EPR2 {yr}")]]
    print(f"\nnew units to add: {len(fr_nb)}")
    for u, mw, yr in fr_nb:
        print(f"    {u:20s} {mw:6.0f} MW  commissioning {yr}  palier {EPR2_PALIER}")

    if "--write" not in sys.argv:
        print("\n(dry run — pass --write to update the workbook)")
        return 0

    from openpyxl import load_workbook
    wb = load_workbook(wb_path)
    ws = wb[SHEET]
    hdr = {c.value: i + 1 for i, c in enumerate(ws[1])}
    n_cl = 0
    for r in range(2, ws.max_row + 1):
        nm = str(ws.cell(r, hdr["name"]).value)
        if nm in close_of:
            ws.cell(r, hdr["closure_year"]).value = close_of[nm]
            n_cl += 1
    existing = {str(ws.cell(r, hdr["name"]).value) for r in range(2, ws.max_row + 1)}
    n_add = 0
    for u, mw, yr in fr_nb:
        if u in existing:
            continue
        row = ws.max_row + 1
        for col, val in (("unit_id", f"FR_NEWBUILD_{yr}"), ("name", u), ("technology", "nuclear"),
                         ("palier", EPR2_PALIER), ("capacity_mw", float(mw)), ("cooling", "sea"),
                         ("basin", None), ("commissioning_year", int(yr)), ("closure_year", None),
                         ("scenario", "reference")):
            if col in hdr:
                ws.cell(row, hdr[col]).value = val
        n_add += 1
    wb.save(wb_path)
    print(f"\nworkbook updated: {n_cl} closures written, {n_add} new units added")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
