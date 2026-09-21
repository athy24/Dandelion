"""The REFERENCE scenario the sensitivity set runs on: where its artefacts live, how they are stamped, and the checks
every consumer applies before trusting them.

WHAT IT IS. One 20-year scenario whose every year prices at the Monte-Carlo ensemble's centre, so a sensitivity reads
as "the ensemble average, with one assumption changed". Each year y is taken whole from one completed draw d(y), chosen
by scripts/select_reference_weather.py on one ensemble. A run on it must reproduce, year by year, what draw d(y)
priced in its own run; three artefacts carry that, all built from the SAME year -> draw map:

  * the weather cube          archive/REFERENCE_WEATHER_20Y.nc               scripts/build_reference_weather.py
    (neighbour-zone weather is read from it; its 72 h year-boundary taper is the one remaining approximation)
  * the step-v availability   data/lake/availability_mc/<table>/wdraw=901    scripts/build_reference_availability.py
    (each year the nuclear outage calendar and reservoir budget draw d(y) was priced on)
  * the French demand/RES     frame of fr_draw(d(y)) for year y              scripts/run_reference_20y.py
    frame                     (so the RES level anchor, run-of-river rainfall memory and demand temperature lags
                              are draw d(y)'s own, not the stitched cube's)

WHY NOT SIMPLY RE-RUN ON THE STITCHED CUBE. Measured on draws 0-29 (dispatch_model/DECISIONS.md, 2026-09-17): a single
step-v projection on the stitched cube gives every year draw 0's outage calendar (about 1.3 GW of nuclear availability
off d(y)'s on average, up to 4.4 GW); step iv anchors each technology's CF on the WHOLE 20-year cube, and the stitched
cube is less windy than the ensemble, so wind was re-inflated in 18-20 of 20 years; run-of-river and demand features
remember the previous year's weather, which in a stitched cube belongs to another draw.

Every artefact records the map it was built from (`year_to_draw`), the ensemble fingerprint and the sha256 of the map's
meta file; a consumer refuses artefacts that disagree. The fingerprint is ALWAYS the one in the map meta -- never a
digest recomputed from the current disk and environment, which moves with any unrelated edit or environment flag.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]                  # …/PriceModeling/dispatch_model
ROOT = REPO.parent

REF_DIR = ROOT / "archive" / "reference_20y"
MAP_PATH = REF_DIR / "ref_weather_map.json"
META_PATH = REF_DIR / "ref_weather_map.meta.json"
CUBE_PATH = ROOT / "archive" / "REFERENCE_WEATHER_20Y.nc"
DEMAND_REF_PATH = REF_DIR / "fr_demand_ref.json"
FRAME_CACHE = REPO / "scratchpad" / "reference_frames"
MC_OUT = REPO / "reports" / "mc50"
MASTER_SEED = 20260629

#: step-v partition of the stitched availability, and the draw index a reference run uses. One number for both: the
#: stitched partition carries a single draw label, and `nuclear_unavail_daily` resolves the run's `--draw` against it.
#: Outside 0-49 (the ensemble's) and not 900 (the pre-2026-09-17 single-realisation partition, kept for audit).
AVAIL_WDRAW = 901
REF_DRAW = 901

AVAIL_TABLES = ("availability_by_tech", "availability_nuclear_units", "interconnectors", "reservoir_budget")
#: the column that places a row in a calendar year (reservoir weeks by the calendar year of their Monday, which is the
#: key the dispatch uses: fr_availability.load_fr_availability)
TIME_COL = {"availability_by_tech": "day", "availability_nuclear_units": "day", "interconnectors": "day",
            "reservoir_budget": "week_start"}


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_map(map_path: str | Path | None = None, meta_path: str | Path | None = None) -> tuple[dict[int, int], dict]:
    """(year -> draw, meta). Refuses a map that disagrees with its own meta. Default paths are read at call time."""
    map_path, meta_path = Path(map_path or MAP_PATH), Path(meta_path or META_PATH)
    if not map_path.exists() or not meta_path.exists():
        raise SystemExit(f"reference map absent: {map_path} / {meta_path.name} "
                         f"(run scripts/select_reference_weather.py)")
    sel = {int(k): int(v) for k, v in json.loads(map_path.read_text(encoding="utf-8")).items()}
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if {int(k): int(v) for k, v in meta.get("map", {}).items()} != sel:
        raise SystemExit(f"{map_path.name} does not match the map recorded in {meta_path.name}")
    for key in ("model_fingerprint", "draws"):
        if key not in meta:
            raise SystemExit(f"{meta_path.name} has no `{key}`")
    ys = sorted(sel)
    if ys != list(range(ys[0], ys[-1] + 1)):
        raise SystemExit(f"{map_path.name}: years are not contiguous: {ys}")
    return sel, meta


def draw_complete(d: int, fp: str, mc: Path | None = None) -> bool:
    m = Path(mc or MC_OUT) / f"draw_{d:03d}" / ".draw_complete"
    return m.exists() and m.read_text(encoding="utf-8").strip() == fp


def mc_work_dir(fp: str) -> Path:
    return REPO / "scratchpad" / "mc" / f"mc50-{fp}"


def cube_attrs(path: str | Path | None = None) -> dict:
    import xarray as xr
    with xr.open_dataset(path or CUBE_PATH) as ds:
        return dict(ds.attrs)


def verify_cube(sel: dict[int, int], meta: dict, path: str | Path | None = None,
                meta_path: str | Path | None = None) -> list[str]:
    """Problems that make the cube at `path` NOT the reference weather of this map (empty list = consistent)."""
    path, meta_path = Path(path or CUBE_PATH), Path(meta_path or META_PATH)
    if not path.exists():
        return [f"reference weather absent: {path}"]
    a = cube_attrs(path)
    problems = []
    if str(a.get("model_fp")) != str(meta["model_fingerprint"]):
        problems.append(f"cube model_fp {a.get('model_fp')!r} != map ensemble {meta['model_fingerprint']}")
    try:
        ytd = {int(k): int(v) for k, v in json.loads(a.get("year_to_draw", "{}")).items()}
    except ValueError:
        ytd = {}
    if ytd != sel:
        problems.append("cube year_to_draw differs from the map")
    if str(a.get("map_meta_sha256")) != file_sha256(meta_path):
        problems.append("cube was built from another version of the map meta")
    if str(a.get("year_map_verified")) not in ("1", "True", "true"):
        problems.append("cube was built from an unverified map (--allow-unverified-map)")
    return problems


def provenance_path(table: str, wdraw: int = AVAIL_WDRAW) -> Path:
    """PROVENANCE.json beside the stitched partition of `table` (build_reference_availability.py writes it)."""
    from powersim_core import lake
    return lake.table_path("availability_mc", table, wdraw=wdraw).with_name("PROVENANCE.json")


def read_provenance(table: str, wdraw: int = AVAIL_WDRAW) -> dict:
    """The stitch's PROVENANCE.json, or {} when absent or unreadable (a killed write): never an exception."""
    try:
        return json.loads(provenance_path(table, wdraw).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def verify_availability(sel: dict[int, int], meta: dict, wdraw: int = AVAIL_WDRAW,
                        meta_path: str | Path | None = None) -> list[str]:
    """Problems that make availability_mc/wdraw=<wdraw> NOT the stitched availability of this map."""
    from powersim_core import lake
    meta_path = Path(meta_path or META_PATH)
    problems = []
    for t in AVAIL_TABLES:
        part, prov = lake.table_path("availability_mc", t, wdraw=wdraw), provenance_path(t, wdraw)
        if not part.exists() or not prov.exists():
            problems.append(f"{t}: stitched partition or PROVENANCE.json absent under wdraw={wdraw}")
            continue
        p = read_provenance(t, wdraw)
        if not p:
            problems.append(f"{t}: PROVENANCE.json unreadable")
            continue
        if {int(k): int(v) for k, v in p.get("map", {}).items()} != sel:
            problems.append(f"{t}: stitched from another year map")
        if p.get("model_fingerprint") != meta["model_fingerprint"]:
            problems.append(f"{t}: stitched from ensemble {p.get('model_fingerprint')}")
        if p.get("map_meta_sha256") != file_sha256(meta_path):
            problems.append(f"{t}: stitched from another version of the map meta")
        if p.get("stitched_sha256") != file_sha256(part):
            problems.append(f"{t}: part.parquet was rewritten after stitching (e.g. a step-v re-projection)")
    return problems


def availability_digest(wdraw: int = AVAIL_WDRAW) -> str:
    """One sha256 over the four PROVENANCE files: what a variant fingerprint records of the availability it read."""
    h = hashlib.sha256()
    for t in AVAIL_TABLES:
        p = provenance_path(t, wdraw)
        h.update(t.encode())
        h.update(p.read_bytes() if p.exists() else b"ABSENT")
    return h.hexdigest()
