"""Give the French coal fleet its closure years, and drop the one duplicated registry row.

The DB build that `repair_fleet_registry.py` restores carries correct nameplates and NO closure year for
any non-nuclear unit. So twelve coal plants -- Blénod, Bouchain, Vitry, La Maxe, all shut a decade ago --
were modelled as operating across a 2027-2046 horizon, and the registry read 4.64 GW of French coal against
a real ~1.8 GW even after the capacity repair. The scenario's own `dispatch_tyndp` cap_coal_gw for France
runs 3.97 GW (2019) -> 1.00 (2025) -> **0.00 from 2027**, so no coal unit may survive into the horizon.

THIS IS THE SAME DEFECT AS THE NUCLEAR ONE, on a different technology: a fleet that never retires because
the column that would retire it is empty. It is fixed separately because `repair_fleet_registry.py`
regenerates the sheet FROM the DB, and re-running it would discard whatever has been added since.

WHY IT DOES NOT MOVE A PRICE TODAY, and is worth fixing anyway. The dispatch reads exactly one technology
out of `availability_by_tech` -- `fr_availability.py` filters `technology == "nuclear"` -- so the other
seven are computed and discarded. The reservoir budget it also reads carries no registry capacity and
provably never binds. So coal availability reaches no price, today. It is a latent trap: real the moment
anyone reads another technology out of that table, invisible until then, and that is precisely the shape of
the four defects this chain has already produced.

DATES ARE AN ASSUMPTION. There is no source file in this repo for French thermal closures. They are stated
from the public record, listed unit by unit so each can be challenged on its own, with the scenario's own
2027 coal exit as the backstop. Only the ordering within 2027 matters to a horizon that opens in 2027; the
earlier dates are recorded so a unit that shut in 2015 does not read as one that shut on the first day of
the projection.

Idempotent: re-running changes nothing once applied.

    python -X utf8 scripts/repair_thermal_fleet.py [--write]
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from availability_model.config import load_config  # noqa: E402

SHEET = "avail_fleet_registry"

#: name -> the year the unit stops producing (`_operating_mask` zeroes it from that year inclusive)
FR_COAL_CLOSURES = {
    "BLENOD 3": 2014, "BLENOD 4": 2014,        # EDF, coal retired; the site kept its CCGT
    "BOUCHAIN 1": 2015,                        # EDF, replaced on site by the Bouchain CCGT (2016)
    "VITRY 3": 2015, "VITRY 4": 2015,          # EDF
    "MAXE 1": 2015, "MAXE 2": 2015,            # EDF (La Maxe)
    "HAVRE 4": 2021,                           # EDF, closed April 2021
    "PROVENCE 5": 2021,                        # GazelEnergie Gardanne, coal ceased 2021
    "SAINT AVOLD 6": 2022,                     # GazelEnergie (Emile Huchet 6), March 2022; briefly
    #                                            recalled as a winter reserve, which is not modelled
    "CORDEMAIS 4": 2027, "CORDEMAIS 5": 2027,  # EDF, ran under a security-of-supply derogation; retired
    #                                            at the scenario's own coal exit
}

#: `PROVENCE 4 BIOMASSE` appears TWICE, at 158 MW and at 9 MW. The Gardanne biomass conversion is a single
#: ~150 MW unit, so the 9 MW row is spurious. It is the only duplicated name in all 168 rows.
DROP_DUPLICATE = ("PROVENCE 4 BIOMASSE", 9.0)


def main() -> int:
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.yaml"))
    from powersim_core.scenario import resolve as resolve_wb
    wb_path = resolve_wb(cfg.resolve(cfg.section("assumptions")["workbook"]))
    print(f"workbook: {wb_path}")

    reg = pd.read_excel(wb_path, sheet_name=SHEET)
    coal = reg[reg.technology == "coal"]
    print(f"\ncoal before: {len(coal)} units, {coal.capacity_mw.sum() / 1e3:.2f} GW, "
          f"{int(coal.closure_year.notna().sum())} with a closure year")

    missing = [n for n in FR_COAL_CLOSURES if not (reg["name"].astype(str) == n).any()]
    unlisted = sorted(set(coal["name"].astype(str)) - set(FR_COAL_CLOSURES))
    if missing:
        print(f"  NAMES NOT FOUND in the registry: {missing}")
    if unlisted:
        print(f"  COAL UNITS WITH NO CLOSURE STATED: {unlisted}  <-- they would survive to 2046")

    print(f"\n{'unit':20s}{'MW':>8s}{'closure':>9s}")
    for n, y in sorted(FR_COAL_CLOSURES.items(), key=lambda kv: (kv[1], kv[0])):
        m = reg["name"].astype(str) == n
        if m.any():
            print(f"{n:20s}{float(reg.loc[m, 'capacity_mw'].iloc[0]):8.0f}{y:9d}")

    dn, dmw = DROP_DUPLICATE
    dup = (reg["name"].astype(str) == dn) & (reg["capacity_mw"].round(1) == dmw)
    print(f"\nduplicate row to drop: {dn} @ {dmw:.0f} MW -> {'found' if dup.any() else 'ALREADY ABSENT'}")

    if "--write" not in sys.argv:
        print("\n(dry run — pass --write to update the workbook)")
        return 0

    from openpyxl import load_workbook
    wb = load_workbook(wb_path)
    ws = wb[SHEET]
    hdr = {c.value: i + 1 for i, c in enumerate(ws[1])}
    n_set, n_del = 0, 0
    for r in range(ws.max_row, 1, -1):                       # descending: deleting shifts the rows below
        nm = str(ws.cell(r, hdr["name"]).value)
        cap = ws.cell(r, hdr["capacity_mw"]).value
        if nm == dn and cap is not None and abs(float(cap) - dmw) < 0.5:
            ws.delete_rows(r)
            n_del += 1
            continue
        if nm in FR_COAL_CLOSURES and str(ws.cell(r, hdr["technology"]).value) == "coal":
            ws.cell(r, hdr["closure_year"]).value = int(FR_COAL_CLOSURES[nm])
            n_set += 1
    wb.save(wb_path)
    print(f"\nworkbook updated: {n_set} coal closures written, {n_del} duplicate row(s) removed, "
          f"{ws.max_row - 1} rows now present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
