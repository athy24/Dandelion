"""Rewrite `avail_fleet_registry` from the DB-built registry, because the workbook copy was not capacity.

WHAT WAS WRONG. `load_scenario_registry` prefers the workbook sheet over `io.fleet.build_fleet_registry`,
so users can carry closures and new builds by hand. The sheet in the workbook was a corrupted snapshot of
that same registry -- identical 168 unit_ids in identical order, but with values that are not nameplates:

    technology        workbook      DB build     ratio
    nuclear           259.34 GW     65.76 GW     3.94      (Cattenom 1: 32,470 MW for a 1310 MW reactor)
    oil                28.54 GW      5.94 GW     4.80
    hydro_pumped        7.12 GW      4.80 GW     1.48
    gas                10.98 GW      8.57 GW     1.28

and the error is per-unit, not a scale: the ratios run 1.00 to 24.8, with Penly 1, Civaux 1, Golfech 1 and
Flamanville 3 exactly right while their neighbours are 5x out. A column that is correct for some rows and
24x wrong for others is a SUM OVER AN OBSERVED SERIES, not a nameplate -- the multiplier being however many
observations that unit had.

WHY IT MATTERED. `_assemble_draw` seeds every unit at `capacity_mw` and reduces from there, so the
availability of a technology is the capacity-WEIGHTED survival of its units. With these weights one reactor
carried 12.5 % of the fleet against a true 2.1 %: when it tripped the dispatch removed 8 GW, and when a
physically identical reactor tripped it removed 1.4 GW. Herfindahl effective unit count 31.1 against 59
real reactors.

AND IT WAS NOT ONLY CAPACITY. The sheet also lost `closure_year` for ALL 168 rows -- so Fessenheim 1 and 2,
shut in February and June 2020, kept running to 2046 -- and six reactors lost the `palier` that assigns
their refuelling class, so they never refuelled. A capacity-only patch would have left both.

THE REPAIR is therefore the whole sheet, not one column: `build_fleet_registry` is the authority and the
workbook is regenerated from it. Hand edits (a closure the user added, a new build) are LISTED before being
overwritten so nothing is lost silently -- if the list is non-empty, decide what to re-apply before saying
yes.

    python -X utf8 scripts/repair_fleet_registry.py [--write]
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from availability_model.config import load_config  # noqa: E402
from availability_model.io.fleet import build_fleet_registry  # noqa: E402

SHEET = "avail_fleet_registry"
#: columns the workbook sheet carries, in its own order
COLS = ["unit_id", "name", "technology", "palier", "capacity_mw", "cooling", "basin",
        "commissioning_year", "closure_year", "scenario"]



def main() -> int:
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.yaml"))
    from powersim_core.scenario import resolve as resolve_wb
    wb_path = resolve_wb(cfg.resolve(cfg.section("assumptions")["workbook"]))
    print(f"workbook: {wb_path}")

    db = build_fleet_registry(cfg)
    old = pd.read_excel(wb_path, sheet_name=SHEET)

    # REFUSE TO DISCARD ROWS THAT ARE NOT IN THE DB. This script rebuilds the sheet FROM the DB build, so
    # any unit added to the workbook afterwards -- the six EPR2 units `sync_nuclear_fleet.py` writes, a
    # hand-entered new build -- would simply vanish. That is the class of silent loss this whole repair
    # exists to undo, so it stops here rather than being reported after the fact.
    extra = sorted(set(old["unit_id"].astype(str)) - set(db["unit_id"].astype(str)))
    if extra and "--force" not in sys.argv:
        names = old.set_index("unit_id")["name"].astype(str)
        raise SystemExit(
            f"the sheet carries {len(extra)} unit(s) the DB build does not: "
            f"{[names.get(u, u) for u in extra]}. Regenerating would delete them. Re-apply them after "
            f"(scripts/sync_nuclear_fleet.py) and pass --force if that is what you intend.")
    print(f"DB build {len(db)} rows | workbook {len(old)} rows | "
          f"same unit_id set: {set(db.unit_id) == set(old.unit_id)}")

    a = old.set_index("unit_id")
    b = db.set_index("unit_id")
    print(f"\n{'technology':18s}{'workbook GW':>13s}{'DB GW':>10s}{'ratio':>8s}")
    for t in sorted(set(old.technology)):
        wg = old.loc[old.technology == t, "capacity_mw"].sum() / 1e3
        dg = db.loc[db.technology == t, "capacity_mw"].sum() / 1e3
        print(f"{t:18s}{wg:13.2f}{dg:10.2f}{(wg / dg if dg else float('nan')):8.2f}")

    # hand edits that regeneration would discard: any non-capacity cell the workbook sets and the DB does
    # not agree with. Capacity is excluded because that is the column being repaired.
    lost = []
    for c in ("closure_year", "commissioning_year", "palier", "scenario"):
        if c not in a.columns or c not in b.columns:
            continue
        for uid in a.index.intersection(b.index):
            av, bv = a.at[uid, c], b.at[uid, c]
            if pd.notna(av) and (pd.isna(bv) or str(av) != str(bv)):
                lost.append((uid, str(a.at[uid, "name"]), c, av, bv))
    print(f"\nworkbook cells the DB does not reproduce (would be lost): {len(lost)}")
    for _uid, nm, c, av, bv in lost[:15]:
        print(f"    {nm:16s} {c:20s} workbook={av!r}  DB={bv!r}")

    out = db.reindex(columns=[c for c in COLS if c in db.columns]).copy()
    if "scenario" not in out.columns:
        out["scenario"] = "reference"
    nuc = out[out.technology == "nuclear"]
    print(f"\nrepaired nuclear: {len(nuc)} units, {nuc.capacity_mw.sum() / 1e3:.2f} GW, "
          f"per-unit {nuc.capacity_mw.min():.0f}-{nuc.capacity_mw.max():.0f} MW")
    print(f"closures carried: "
          f"{out.loc[out.closure_year.notna(), ['name', 'closure_year']].to_dict('records')}")

    if "--write" not in sys.argv:
        print("\n(dry run — pass --write to update the workbook)")
        return 0

    from openpyxl import load_workbook
    wb = load_workbook(wb_path)
    ws = wb[SHEET]
    hdr = {c.value: i + 1 for i, c in enumerate(ws[1])}
    by_uid = {str(r.unit_id): r for r in out.itertuples(index=False)}
    n = 0
    for r in range(2, ws.max_row + 1):
        uid = str(ws.cell(r, hdr["unit_id"]).value)
        rec = by_uid.get(uid)
        if rec is None:
            continue
        for c, col in hdr.items():
            if c in ("unit_id",) or not hasattr(rec, str(c)):
                continue
            v = getattr(rec, str(c))
            ws.cell(r, col).value = None if pd.isna(v) else (
                int(v) if c in ("commissioning_year", "closure_year") else
                (float(v) if c == "capacity_mw" else v))
        n += 1
    wb.save(wb_path)
    print(f"\nworkbook updated: {n} rows rewritten in {SHEET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
