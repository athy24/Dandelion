"""Stitch the reference scenario's step-v availability, year by year, from the Monte-Carlo draws' own partitions.

Draw d(y) priced year y on `availability_mc/wdraw=d(y)`: its nuclear outage calendar (planned, forced and common-mode
streams, seeded by the draw id), its thermal derating and its weekly reservoir budget (wetness normalised on ITS cube's
20-year precipitation). Re-projecting step v once on the stitched weather reproduces none of that -- other seeds,
wetness normalised on another cube -- and was measured 1.3 GW of nuclear availability off d(y) on average, 4.4 GW in the
worst year. So the partition is not projected: it is COPIED, each calendar year from its own draw.

  availability_by_tech, availability_nuclear_units, interconnectors   rows whose `day` falls in year y
  reservoir_budget                                                    rows whose `week_start` falls in year y
                                                                      (the dispatch keys weeks on that year)

THE LABEL. Every partition carries one `draw` label and the dispatch resolves the run's `--draw` against the labels it
finds (fr_availability.nuclear_unavail_daily): a stitch keeping its source labels would find one year and silently
price the other nineteen on the 2019 proxy. Every row is relabelled to the partition number (901), which is also the
draw index a reference run uses. reservoir_budget has no label.

SOURCES ARE CHECKED, the result is VERIFIED. A source must belong to a draw completed under the map's ensemble
fingerprint and must predate that draw's completion marker. After stitching, each year's nuclear availability (Kd) and
mean weekly reservoir budget must equal the source draw's exactly, and the calendar must equal the source's. The
partitions are written through a temp name and a PROVENANCE.json beside each (map, fingerprint, map-meta sha, source
sha256 and mtimes, stitched sha256) is written LAST; consumers refuse a partition whose sha no longer matches it.

Idempotent: if every PROVENANCE already matches the map and every source is unchanged, nothing is rewritten.

    python -X utf8 -W ignore scripts/build_reference_availability.py [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import reference_scenario as rs  # noqa: E402


def _source_path(table: str, d: int) -> Path:
    from powersim_core import lake
    return lake.table_path("availability_mc", table, wdraw=d)


def check_sources(sel: dict[int, int], fp: str, mc: Path | None = None) -> dict[int, dict]:
    """{draw: {table: {sha256, mtime_ns}}} for every source partition, refusing any that cannot be trusted."""
    mc = Path(mc or rs.MC_OUT)
    out: dict[int, dict] = {}
    for d in sorted(set(sel.values())):
        marker = Path(mc) / f"draw_{d:03d}" / ".draw_complete"
        if not rs.draw_complete(d, fp, mc):
            raise SystemExit(f"draw {d} is not complete under ensemble {fp}: its availability cannot be used")
        out[d] = {}
        for t in rs.AVAIL_TABLES:
            p = _source_path(t, d)
            if not p.exists():
                raise SystemExit(f"source partition absent: {p}")
            st = p.stat()
            if st.st_mtime_ns > marker.stat().st_mtime_ns:
                raise SystemExit(f"{p} was rewritten after draw {d} completed: not the availability it priced on")
            out[d][t] = {"sha256": rs.file_sha256(p), "mtime_ns": int(st.st_mtime_ns)}
    return out


def _union_categories(frames: list[pd.DataFrame]) -> list[pd.DataFrame]:
    """Categorical columns (nuclear units' `state`) keep one categorical dtype across draws whose categories differ."""
    cats: dict[str, set] = {}
    for f in frames:
        for c in f.columns:
            if isinstance(f[c].dtype, pd.CategoricalDtype):
                cats.setdefault(c, set()).update(map(str, f[c].cat.categories))
    for c, s in cats.items():
        dtype = pd.CategoricalDtype(sorted(s))
        for f in frames:
            f[c] = f[c].astype(str).astype(dtype)
    return frames


def stitch_table(table: str, sel: dict[int, int], label: int) -> pd.DataFrame:
    col = rs.TIME_COL[table]
    frames, first_cols = [], None
    for y in sorted(sel):
        src = pd.read_parquet(_source_path(table, sel[y]))          # pandas: no hive `wdraw` column is added
        if first_cols is None:
            first_cols = list(src.columns)
        part = src[pd.to_datetime(src[col]).dt.year == y].copy()
        if part.empty:
            raise SystemExit(f"{table}: draw {sel[y]} has no rows in {y}")
        frames.append(part)
    frames = _union_categories(frames)
    out = pd.concat(frames, ignore_index=True)
    if "draw" in out.columns:
        out["draw"] = np.int64(label)
    keys = [c for c in (col, "technology", "unit_id", "border", "direction") if c in out.columns]
    out = out.sort_values(keys, kind="mergesort").reset_index(drop=True)
    return out[first_cols]


def _nuclear_kd(frame: pd.DataFrame, year: int) -> float:
    n = frame[(frame["technology"] == "nuclear") & (pd.to_datetime(frame["day"]).dt.year == year)]
    return float(n["available_mw"].sum() / n["installed_mw"].sum())


def _mean_reservoir(frame: pd.DataFrame, year: int) -> float:
    return float(frame.loc[pd.to_datetime(frame["week_start"]).dt.year == year, "avail_energy_gwh"].mean())


def verify_stitch(stitched: dict[str, pd.DataFrame], sel: dict[int, int], label: int) -> pd.DataFrame:
    """Per-year FR nuclear Kd and mean weekly reservoir budget, asserted equal to the source draw's."""
    rows = []
    bt, rb = stitched["availability_by_tech"], stitched["reservoir_budget"]
    if "draw" in bt.columns and set(bt["draw"].unique()) != {label}:
        raise SystemExit(f"availability_by_tech carries labels {sorted(bt['draw'].unique())}, not only {label}")
    if bt["installed_mw"].isna().any():
        raise SystemExit("availability_by_tech: installed_mw has missing values")
    if bt.duplicated(["day", "technology"]).any():
        raise SystemExit("availability_by_tech: duplicated (day, technology)")
    first = pd.read_parquet(_source_path("availability_by_tech", sel[min(sel)]))
    if set(pd.to_datetime(bt["day"])) != set(pd.to_datetime(first["day"])):
        raise SystemExit("availability_by_tech: stitched calendar differs from the source partitions'")
    first_rb = pd.read_parquet(_source_path("reservoir_budget", sel[min(sel)]))
    if sorted(pd.to_datetime(rb["week_start"])) != sorted(pd.to_datetime(first_rb["week_start"])):
        raise SystemExit("reservoir_budget: stitched weeks differ from the source partitions'")
    for y in sorted(sel):
        d = sel[y]
        src_bt = pd.read_parquet(_source_path("availability_by_tech", d))
        src_rb = pd.read_parquet(_source_path("reservoir_budget", d))

        kd_s, kd_d = _nuclear_kd(bt, y), _nuclear_kd(src_bt, y)
        rb_s, rb_d = _mean_reservoir(rb, y), _mean_reservoir(src_rb, y)
        if not (np.isclose(kd_s, kd_d, rtol=0, atol=1e-12) and np.isclose(rb_s, rb_d, rtol=0, atol=1e-9)):
            raise SystemExit(f"{y}: stitched Kd {kd_s:.6f} / reservoir {rb_s:.3f} "
                             f"!= draw {d}'s {kd_d:.6f} / {rb_d:.3f}")
        rows.append({"year": y, "draw": d, "nuclear_kd": round(kd_s, 4), "reservoir_gwh_week": round(rb_s, 1)})
    return pd.DataFrame(rows).set_index("year")


def build(sel: dict[int, int], meta: dict, wdraw: int = rs.AVAIL_WDRAW, force: bool = False,
          meta_path: Path | None = None, mc: Path | None = None) -> bool:
    """Stitch and write availability_mc/*/wdraw=<wdraw>. Returns False when it was already up to date."""
    from powersim_core import lake
    meta_path = Path(meta_path or rs.META_PATH)
    fp = meta["model_fingerprint"]
    if wdraw in range(50) or wdraw == 900 or wdraw in set(sel.values()) or wdraw in set(meta.get("draws", [])):
        raise SystemExit(f"wdraw={wdraw} belongs to a Monte-Carlo draw or to the previous reference: the stitch "
                         f"would overwrite it (use {rs.AVAIL_WDRAW})")
    sources = check_sources(sel, fp, mc)
    meta_sha = rs.file_sha256(meta_path)
    if not force and not rs.verify_availability(sel, meta, wdraw, meta_path):
        same = all(rs.read_provenance(t, wdraw).get("sources")
                   == {str(d): s[t] for d, s in sources.items()} for t in rs.AVAIL_TABLES)
        if same:
            print(f"availability_mc/*/wdraw={wdraw} is already the stitch of this map - nothing to do")
            return False
    stitched = {t: stitch_table(t, sel, wdraw) for t in rs.AVAIL_TABLES}
    report = verify_stitch(stitched, sel, wdraw)
    for t, df in stitched.items():
        part = lake.table_path("availability_mc", t, wdraw=wdraw)
        part.parent.mkdir(parents=True, exist_ok=True)
        rs.provenance_path(t, wdraw).unlink(missing_ok=True)          # stale until the new stitch is in place
        tmp = part.with_name(".part.parquet.tmp")
        df.to_parquet(tmp, compression="zstd", index=False)
        os.replace(tmp, part)
    for t, df in stitched.items():                                     # provenance LAST
        part = lake.table_path("availability_mc", t, wdraw=wdraw)
        prov = {"artefact": "reference scenario step-v availability, stitched year by year",
                "wdraw": wdraw, "label": wdraw, "model_fingerprint": fp, "ensemble_draws": meta["draws"],
                "map": {str(y): d for y, d in sorted(sel.items())}, "map_meta_sha256": meta_sha,
                "sources": {str(d): s[t] for d, s in sources.items()}, "rows": int(len(df)),
                "stitched_sha256": rs.file_sha256(part),
                "created_utc": datetime.now(UTC).isoformat(timespec="seconds")}
        pp = rs.provenance_path(t, wdraw)
        tmp = pp.with_name(pp.name + ".tmp")
        tmp.write_text(json.dumps(prov, indent=1), encoding="utf-8")
        os.replace(tmp, pp)
    pd.set_option("display.width", 160)
    print(report.to_string())
    print(f"wrote availability_mc/*/wdraw={wdraw} ({len(set(sel.values()))} source draws, label {wdraw})")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default=None, help="default: archive/reference_20y/ref_weather_map.json")
    ap.add_argument("--meta", default=None, help="default: the map's .meta.json")
    ap.add_argument("--wdraw", type=int, default=rs.AVAIL_WDRAW)
    ap.add_argument("--force", action="store_true", help="re-stitch even if the partition already matches the map")
    a = ap.parse_args()
    sel, meta = rs.load_map(a.map, a.meta)
    build(sel, meta, a.wdraw, a.force, a.meta)
    problems = rs.verify_availability(sel, meta, a.wdraw, a.meta)
    if problems:
        raise SystemExit("stitched availability does not verify: " + "; ".join(problems))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
