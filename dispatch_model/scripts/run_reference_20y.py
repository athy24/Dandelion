"""Run the 20-year projection on the REFERENCE scenario: each year y exactly as Monte-Carlo draw d(y) priced it.

`run_projection_20y.py` prices one weather realisation: France's hourly demand, RES and national temperature come
from `weather_shapes.fr_draw(draw)`, which runs steps iii and iv over the WHOLE 20-year horizon of one cube. Three
things in that computation cross a year boundary: step iv scales each technology's capacity factor so its 20-year
mean hits the calibrated level (`Projector._anchor`), run-of-river remembers about 90 days of rain, and demand
features lag temperature by one to two days. Run on a cube stitched from 15 draws, all three would see the other
draws' weather: measured on draws 0-29, the anchor alone moved the FR price by -1 to -2.5 EUR/MWh.

So this wrapper does not let `fr_draw` compute France on the stitched cube. For every draw d the year map names it
computes `fr_draw(d)` -- draw d's own 20-year frame, from the demand and RES driver caches that draw d's own run
wrote (`demand/projection_features` and `res/proj_drivers`, realization=d) -- keeps year y of the frame of d(y),
and places the stitched frame in `weather_shapes._DRAW_CACHE` under the reference draw index (901) before
`run_projection_20y.main()` starts. The neighbours' weather still comes from the reference cube
(archive/REFERENCE_WEATHER_20Y.nc) and the step-v availability from `availability_mc/wdraw=901`, both built from
the same map (scripts/reference_scenario.py).

THE DRIVER CACHES ARE SNAPSHOTTED. The cubes they came from are deleted after each draw, so the caches are the
only record of draw d's French weather drivers, and any later run with `--draw d` would overwrite them. On first
use they are copied to archive/reference_20y/drivers/ -- only if their sidecar names draw d's cube in this
ensemble and they predate the draw's completion marker -- and every frame is computed from the snapshot, with
their sha256 checked. While a frame is computed, every weather-cube read is redirected to a path that does not
exist and every cache freshness check is replaced by a snapshot check that raises on mismatch: a frame can come
from the ensemble's drivers or from nowhere, never silently from another cube.

THE MODEL MUST BE THE ENSEMBLE'S. The frames, and the whole reference run, reproduce draw d(y) only if the code,
configs, fitted models and libraries are the ones that priced it. Every run compares them against the ensemble's
FINGERPRINT.json and refuses on any difference (the workbook, the environment switches and the run parameters
are excluded: those are what a sensitivity variant is allowed to change). A frame depends on the workbook only
through its `demand_*` and `res_*` tabs, so frames are cached on disk keyed by those tabs' content, the drivers'
sha256, the model and the offshore-loss flag: the variants that do not touch France's demand or RES share the
baseline's frames.

WHAT FAILS LOUDLY HERE THAT `run_projection_20y` LETS PASS. A weather-provider exception there falls back to the
2019 weather for the rest of the run; the neighbours' cube reader and their observed-weather history each return {}
on any error, which prices every neighbour on the FR proxy; the availability lookup prints a WARNING and uses the
2019 proxy; a markup model that fails to load leaves every price at raw SMC. In a reference run each of these is a
wrong baseline for every sensitivity, so each is turned into an exit. So is an environment switch the ensemble did
not run with, unless the caller declares it as the variant's own (`--allow-env`, which run_sensitivities passes).

    python -u -X utf8 -W ignore scripts/run_reference_20y.py --out DIR [--start Y --end Y | --years ..] [--n-weeks N]
    python -u -X utf8 -W ignore scripts/run_reference_20y.py --frames-only       # snapshot + frames, no dispatch
    python -u -X utf8 -W ignore scripts/run_reference_20y.py --measure-demand   # -> archive/reference_20y/

Run from dispatch_model/ (relative paths are resolved there, as in run_projection_20y). The environment is set
here: DISPATCH_AVAIL_WDRAW=901, POWERSIM_WEATHER_CUBE=the reference cube, POWERSIM_RES_OFFSHORE_LOSSES=1; a
conflicting value already set is refused. POWERSIM_SCENARIOS selects a variant's workbook as everywhere else.

PYTHONHASHSEED MUST BE PINNED for a run (run_sensitivities sets 0). `flexibility/storage.py` builds the storage block
by iterating a set of zone names, so under Python's per-process hash randomisation every process solves a permuted
LP: the same optimum, but at degenerate hours possibly another vertex, other prices, another seam path for the rest of
the year. A pinned seed gives the baseline and every variant one LP ordering, so a variant's difference is its
assumption and not the permutation. It is part of the run identity. Several processes may run disjoint `--years`
into one --out (the baseline does): the stamp is written through a per-process temp name.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import reference_scenario as rs  # noqa: E402

#: the two per-realization driver caches `fr_draw` reads, as (layer, dataset)
DRIVER_TABLES = (("demand", "projection_features"), ("res", "proj_drivers"))
DRIVER_SNAPSHOT = rs.REF_DIR / "drivers"
#: where cube reads are pointed while a frame is computed: it must not exist
NO_CUBE = rs.REF_DIR / "NO_CUBE__frames_are_read_from_the_driver_snapshot.nc"
#: fingerprint components a variant may change, and so not part of the model comparison
_VARIANT_KEYS = ("env:", "workbook:", "param:")
FRAME_COLUMNS = ["load_mw", "res_mw", "temp_nat"]
FRAME_FORMAT = 1
#: switches that change how a run reports, not what it prices: a resume may differ in them
_RUN_ONLY_ENV = frozenset({"POWERSIM_NO_PROGRESS", "DISPATCH_CAPTURE_DISPATCH"})
#: switches this wrapper sets or checks itself, or that name a draw/workbook rather than a model
_MANAGED_ENV = frozenset({"POWERSIM_WEATHER_CUBE", "DISPATCH_AVAIL_WDRAW", "DISPATCH_CAPTURE_DISPATCH",
                          "POWERSIM_SCENARIOS", "POWERSIM_RES_OFFSHORE_LOSSES", "POWERSIM_NO_PROGRESS"})


# ---- the model --------------------------------------------------------------------------------------------------
def ensemble_components(fp: str) -> dict[str, str]:
    """The ensemble's recorded fingerprint components, checked against its own digest."""
    from model_fingerprint import digest_of
    f = rs.mc_work_dir(fp) / "FINGERPRINT.json"
    if not f.exists():
        raise SystemExit(f"{f} absent: the ensemble's model cannot be compared")
    comp = json.loads(f.read_text(encoding="utf-8"))["components"]
    if digest_of(comp) != fp:
        raise SystemExit(f"{f} does not hash to {fp}")
    return comp


def model_drift(fp: str, now: dict[str, str] | None = None) -> list[str]:
    """Fingerprint components that differ between the ensemble and the model on disk, variant keys excluded."""
    from model_fingerprint import components
    rec = ensemble_components(fp)
    now = components() if now is None else now
    keys = {k for k in set(rec) | set(now) if not k.startswith(_VARIANT_KEYS)}
    return sorted(k for k in keys if rec.get(k) != now.get(k))


def env_drift(comp: dict[str, str], allowed: set[str], environ: dict[str, str] | None = None) -> list[str]:
    """DISPATCH_*/POWERSIM_* switches that differ from those the ensemble ran with, apart from the ones this wrapper
    manages and the variant's own (`allowed`). The ensemble's are its `env:` fingerprint components."""
    environ = dict(os.environ) if environ is None else environ
    rec = {k[4:]: v for k, v in comp.items() if k.startswith("env:")}
    now = {k: v for k, v in environ.items()
           if k.startswith(("DISPATCH_", "POWERSIM_")) and k not in _MANAGED_ENV and k not in allowed}
    rec = {k: v for k, v in rec.items() if k not in allowed}
    return sorted(k for k in set(rec) | set(now) if rec.get(k) != now.get(k))


def require_ensemble_model(fp: str) -> dict[str, str]:
    drift = model_drift(fp)
    if drift:
        shown = ", ".join(drift[:8]) + (f" (+{len(drift) - 8} more)" if len(drift) > 8 else "")
        raise SystemExit(f"the model on disk is not the one ensemble {fp} priced with: {shown}. A reference run "
                         f"would not reproduce its draws; select a map on an ensemble of this model")
    return ensemble_components(fp)


def workbook_path() -> Path:
    from powersim_core.scenario import resolve
    return Path(resolve(rs.ROOT / "scenarios.xlsx")).resolve()


def fr_sheets_digest(path: str | Path) -> str:
    """sha256 of the `demand_*` and `res_*` tabs as pandas reads them: all a frame takes from the workbook."""
    import pandas as pd
    xl = pd.read_excel(path, sheet_name=None)
    tabs = sorted(n for n in xl if n.startswith(("demand_", "res_")))
    if not any(n.startswith("demand_") for n in tabs) or not any(n.startswith("res_") for n in tabs):
        raise SystemExit(f"{path}: no demand_* or res_* tabs (legacy workbooks are not supported here)")
    h = hashlib.sha256()
    for n in tabs:
        df = xl[n]
        h.update(n.encode())
        h.update(json.dumps([str(c) for c in df.columns]).encode())
        h.update(json.dumps([str(t) for t in df.dtypes]).encode())
        h.update(pd.util.hash_pandas_object(df, index=True).to_numpy().tobytes())
    return h.hexdigest()


def offshore_flag() -> str:
    return os.environ.get("POWERSIM_RES_OFFSHORE_LOSSES", "").strip().lower()


# ---- the driver snapshot ----------------------------------------------------------------------------------------
def _snap_part(layer: str, dataset: str, d: int) -> Path:
    return DRIVER_SNAPSHOT / f"{layer}__{dataset}" / f"realization={d}" / "part.parquet"


def _sidecar_names_draw(side: Path, fp: str, d: int, mc: Path | None = None) -> bool:
    try:
        p = Path(json.loads(side.read_text(encoding="utf-8"))["path"])
    except (OSError, ValueError, KeyError):
        return False
    return p.name == f"cube_{d:03d}.nc" and p.parent.name == f"{Path(mc or rs.MC_OUT).name}-{fp}"


def ensure_driver_snapshot(draws: list[int], fp: str, mc: Path | None = None) -> dict[str, str]:
    """{"<layer>/<dataset>/realization=<d>": sha256} of the snapshot, taking it from the lake where absent."""
    from powersim_core import lake
    mc = Path(mc or rs.MC_OUT)
    man_f = DRIVER_SNAPSHOT / "MANIFEST.json"
    man = json.loads(man_f.read_text(encoding="utf-8")) if man_f.exists() else {}
    if man and man.get("model_fingerprint") != fp:
        raise SystemExit(f"{man_f} holds the drivers of ensemble {man.get('model_fingerprint')}, not {fp}: move "
                         f"archive/reference_20y aside before building a reference of another ensemble")
    files = man.get("files", {})
    changed = False
    for d in draws:
        marker = Path(mc) / f"draw_{d:03d}" / ".draw_complete"
        for layer, dataset in DRIVER_TABLES:
            key = f"{layer}/{dataset}/realization={d}"
            snap = _snap_part(layer, dataset, d)
            if key in files and snap.exists():
                continue
            src = lake.table_path(layer, dataset, realization=d)
            side = src.with_name("source_cube.json")
            if not (src.exists() and _sidecar_names_draw(side, fp, d, mc)):
                raise SystemExit(f"{src}: not the driver cache draw {d} of {fp} wrote (sidecar), and no snapshot")
            if not rs.draw_complete(d, fp, mc) or src.stat().st_mtime_ns > marker.stat().st_mtime_ns:
                raise SystemExit(f"{src} was rewritten after draw {d} completed (or the draw is not complete)")
            snap.parent.mkdir(parents=True, exist_ok=True)
            tmp = snap.with_name(".part.parquet.tmp")
            shutil.copy2(src, tmp)
            os.replace(tmp, snap)
            shutil.copy2(side, snap.with_name("source_cube.json"))
            files[key] = rs.file_sha256(snap)
            changed = True
    if changed:
        man = {"model_fingerprint": fp, "files": dict(sorted(files.items())),
               "note": "driver caches of the Monte-Carlo draws the reference map names, copied from the lake "
                       "(their cubes are deleted after each draw); read by scripts/run_reference_20y.py",
               "updated_utc": datetime.now(UTC).isoformat(timespec="seconds")}
        man_f.parent.mkdir(parents=True, exist_ok=True)
        man_f.write_text(json.dumps(man, indent=1), encoding="utf-8")
    for d in draws:
        for layer, dataset in DRIVER_TABLES:
            key = f"{layer}/{dataset}/realization={d}"
            if rs.file_sha256(_snap_part(layer, dataset, d)) != files.get(key):
                raise SystemExit(f"driver snapshot {key} does not match its MANIFEST sha256")
    return {k: v for k, v in files.items() if int(k.rsplit("=", 1)[1]) in set(draws)}


@contextlib.contextmanager
def drivers_from_snapshot(draws: set[int]):
    """While active: the two driver caches of `draws` are read from the snapshot, any other cache or cube read
    fails. Patches module attributes the models look up at call time (see the module docstring)."""
    import powersim_core.weather_cube as wc
    from powersim_core import lake

    def _key(layer, dataset, partitions):
        if (layer, dataset) not in DRIVER_TABLES or set(partitions) != {"realization"}:
            return None
        return int(partitions["realization"])

    def fresh(cache_path, cube_path):                        # noqa: ARG001 - the cube is deliberately ignored
        cache_path = Path(cache_path)
        m = re.fullmatch(r"realization=(\d+)", cache_path.parent.name)
        d = int(m.group(1)) if m else None
        ds = (cache_path.parent.parent.parent.name, cache_path.parent.parent.name)
        if d not in draws or ds not in DRIVER_TABLES:
            raise SystemExit(f"frame computation asked for {cache_path}, which is not a snapshotted driver cache")
        return True

    orig_read, orig_fresh, orig_write = lake.read_table, wc.cache_is_fresh, lake.write_table

    def read_table(layer, dataset, **partitions):
        d = _key(layer, dataset, partitions)
        if d is None:
            return orig_read(layer, dataset, **partitions)
        if d not in draws:
            raise SystemExit(f"frame computation asked for {layer}/{dataset} realization={d}, not snapshotted")
        import pandas as pd
        return pd.read_parquet(_snap_part(layer, dataset, d))

    def write_table(df, layer, dataset, **kw):
        raise SystemExit(f"frame computation tried to write {layer}/{dataset} {kw}: it must only read the snapshot")

    old_cube = os.environ.get("POWERSIM_WEATHER_CUBE")
    lake.read_table, wc.cache_is_fresh, lake.write_table = read_table, fresh, write_table
    os.environ["POWERSIM_WEATHER_CUBE"] = str(NO_CUBE)
    try:
        yield
    finally:
        lake.read_table, wc.cache_is_fresh, lake.write_table = orig_read, orig_fresh, orig_write
        if old_cube is None:
            os.environ.pop("POWERSIM_WEATHER_CUBE", None)
        else:
            os.environ["POWERSIM_WEATHER_CUBE"] = old_cube


# ---- frames -----------------------------------------------------------------------------------------------------
def frame_key(d: int, fp: str, sheets: str, snapshot: dict[str, str], comp: dict[str, str], flag: str) -> dict:
    """Everything a draw's FR frame depends on. The model part is the ensemble's (require_ensemble_model)."""
    model = hashlib.sha256(json.dumps({k: v for k, v in sorted(comp.items()) if not k.startswith(_VARIANT_KEYS)},
                                      sort_keys=True).encode()).hexdigest()
    return {"format": FRAME_FORMAT, "draw": d, "model_fingerprint": fp, "model_components_sha256": model,
            "fr_sheets_sha256": sheets, "offshore_losses": flag,
            "drivers": {k: v for k, v in snapshot.items() if k.endswith(f"realization={d}")}}


def _frame_file(key: dict) -> Path:
    h = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:20]
    return rs.FRAME_CACHE / f"frame_d{key['draw']:03d}_{h}.parquet"


def draw_frame(key: dict):
    """Draw d's whole-horizon FR frame [load_mw, res_mw, temp_nat], from the disk cache or computed."""
    import pandas as pd

    from dispatch_model import weather_shapes as wsh
    f = _frame_file(key)
    side = f.with_suffix(".json")
    if f.exists() and side.exists():
        rec = json.loads(side.read_text(encoding="utf-8"))
        if rec.get("key") == key and rec.get("sha256") == rs.file_sha256(f):
            return pd.read_parquet(f)
    d = key["draw"]
    t0 = time.monotonic()
    with drivers_from_snapshot({d}):
        wsh._DRAW_CACHE.pop(d, None)
        try:
            df = wsh.fr_draw(d)[FRAME_COLUMNS].copy()
        finally:
            wsh._DRAW_CACHE.pop(d, None)
    if df.empty or df.isna().any().any() or not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise SystemExit(f"draw {d}: computed FR frame is empty, has NaN or an unordered index")
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    df.to_parquet(tmp)
    os.replace(tmp, f)
    tmp = side.with_name(f".{side.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"key": key, "sha256": rs.file_sha256(f), "rows": len(df),
                               "computed_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                               "seconds": round(time.monotonic() - t0, 1)}, indent=1), encoding="utf-8")
    os.replace(tmp, side)
    print(f"[ref] frame of draw {d} computed in {time.monotonic() - t0:.0f}s -> {f.name}", flush=True)
    return df


def stitch_frames(sel: dict[int, int], frames: dict):
    """Year y of the frame of d(y), for every mapped year, as one hourly frame."""
    import pandas as pd
    parts = []
    for y in sorted(sel):
        f = frames[sel[y]]
        part = f[f.index.year == y]
        if part.empty:
            raise SystemExit(f"frame of draw {sel[y]} has no hours in {y}")
        parts.append(part)
    out = pd.concat(parts)
    if not out.index.is_monotonic_increasing or out.index.has_duplicates:
        raise SystemExit("stitched FR frame index is not strictly increasing")
    return out


def reference_frame(sel: dict[int, int], meta: dict) -> tuple:
    """(stitched frame, provenance) for the workbook in force (POWERSIM_SCENARIOS or scenarios.xlsx)."""
    fp = meta["model_fingerprint"]
    comp = require_ensemble_model(fp)
    draws = sorted(set(sel.values()))
    for d in draws:
        if not rs.draw_complete(d, fp):
            raise SystemExit(f"draw {d} of the map is not complete under ensemble {fp}")
    flag = offshore_flag()
    if flag not in ("1", "true", "yes"):
        raise SystemExit("POWERSIM_RES_OFFSHORE_LOSSES must be on: the ensemble priced offshore with its losses")
    snapshot = ensure_driver_snapshot(draws, fp)
    wb = workbook_path()
    sheets = fr_sheets_digest(wb)
    keys = {d: frame_key(d, fp, sheets, snapshot, comp, "1") for d in draws}
    frames = {d: draw_frame(keys[d]) for d in draws}
    stitched = stitch_frames(sel, frames)
    wb_sha = rs.file_sha256(wb)
    prov = {"workbook": str(wb), "workbook_sha256": wb_sha, "fr_sheets_sha256": sheets,
            "reproduces_ensemble_workbook": wb_sha == comp.get(f"workbook:{rs.ROOT.joinpath('scenarios.xlsx').name}"),
            "frames": {str(d): _frame_file(k).name for d, k in keys.items()},
            "frame_keys_sha256": hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()}
    return stitched, prov


# ---- the run ----------------------------------------------------------------------------------------------------
def _set_env(name: str, value: str, same=lambda a, b: a == b) -> None:
    cur = os.environ.get(name)
    if cur is not None and cur != "" and not same(cur, value):
        raise SystemExit(f"{name}={cur} is set, but a reference run needs {value}")
    os.environ[name] = value


def _same_path(a: str, b: str) -> bool:
    return Path(a).resolve() == Path(b).resolve()


def install_strict_hooks(cfg) -> None:
    """Turn the projection's silent fallbacks into exits (module docstring)."""
    from dispatch_model.rolling import fr_availability as fa

    from dispatch_model import weather_shapes as wsh

    orig_provider = wsh.default_weather_provider

    def provider(*a, **k):
        try:
            return orig_provider(*a, **k)
        except Exception as exc:                                    # noqa: BLE001 - re-raised as an exit
            raise SystemExit(f"[ref] weather provider failed ({type(exc).__name__}: {exc}); a reference run does "
                             f"not fall back to 2019 weather") from exc

    orig_load = fa.load_fr_availability

    def load(config):
        out = orig_load(config)
        want = f"availability_mc/wdraw={rs.AVAIL_WDRAW}"
        if not out or out.get("source") != want or not out.get("reservoir"):
            raise SystemExit(f"[ref] step-v availability not loaded from {want} with a reservoir budget "
                             f"(got {None if not out else out.get('source')})")
        labels = set(out["nuc_frac"].index.get_level_values(0))
        if labels != {rs.REF_DRAW}:
            raise SystemExit(f"[ref] {want} carries draw labels {sorted(labels)}, not [{rs.REF_DRAW}]")
        return out

    orig_nuc = fa.nuclear_unavail_daily

    def nuc(fr_avail, target_year, ref_year, draw, nuc_cap):
        out = orig_nuc(fr_avail, target_year, ref_year, draw, nuc_cap)
        if out is None and nuc_cap > 0:
            raise SystemExit(f"[ref] {target_year}: stitched nuclear availability not applied")
        return out

    from powersim_core.weather_cube import zone_map
    expected = set(zone_map(rs.CUBE_PATH)) - {"FR"}
    orig_hist = wsh.zone_hist_weather

    def hist(config):
        out = orig_hist(config)
        if expected and not (set(out) & expected):
            raise SystemExit("[ref] observed neighbour weather (weathergen/output/zone_hist.parquet) did not load: "
                             "every neighbour would be fitted on the FR proxy")
        return out

    from dispatch_model import markup as mk
    orig_markup = mk.load_model

    def markup(config):
        try:
            out = orig_markup(config)
        except (FileNotFoundError, OSError) as exc:
            raise SystemExit(f"[ref] markup model did not load ({exc}): prices would stay at raw SMC") from exc
        if not out:
            raise SystemExit("[ref] markup model is empty: prices would stay at raw SMC")
        return out

    (wsh.default_weather_provider, wsh.zone_hist_weather, mk.load_model,
     fa.load_fr_availability, fa.nuclear_unavail_daily) = provider, hist, markup, load, nuc

    zw = wsh._draw_zone_weather(cfg, rs.REF_DRAW)
    if not zw or set(zw) != expected:
        raise SystemExit(f"[ref] neighbour weather not read from {rs.CUBE_PATH.name}: zones {sorted(zw)} "
                         f"against {sorted(expected)} in the cube")


def run_identity(sel, meta, prov) -> dict:
    from powersim_core.weather_cube import cube_identity
    return {"reference_draw": rs.REF_DRAW, "model_fingerprint": meta["model_fingerprint"],
            "map_meta_sha256": rs.file_sha256(rs.META_PATH),
            "year_to_draw": {str(y): d for y, d in sorted(sel.items())},
            "cube": cube_identity(rs.CUBE_PATH), "availability_sha256": rs.availability_digest(),
            "workbook_sha256": prov["workbook_sha256"], "frame_keys_sha256": prov["frame_keys_sha256"],
            "env": {k: v for k, v in sorted(os.environ.items())
                    if (k.startswith(("DISPATCH_", "POWERSIM_")) or k == "PYTHONHASHSEED") and k not in _RUN_ONLY_ENV}}


def measure_demand(sel, meta, stitched, prov, out: Path | None = None) -> dict:
    """Write the FR annual demand of the stitched frame (make_sensitivities' demand base); unchanged if identical."""
    out = Path(out or rs.DEMAND_REF_PATH)
    if not prov["reproduces_ensemble_workbook"]:
        raise SystemExit("the demand reference must be measured on the ensemble's own workbook (unset "
                         "POWERSIM_SCENARIOS, and scenarios.xlsx must be the one the ensemble priced)")
    ann = stitched["load_mw"].groupby(stitched.index.year).sum() / 1e6
    res = stitched["res_mw"].groupby(stitched.index.year).sum() / 1e6
    rec = {"what": "FR annual demand (TWh) of the reference scenario, as fr_draw computes it: year y of draw d(y)",
           "fr_demand_twh": {str(int(y)): round(float(v), 4) for y, v in ann.items()},
           "fr_res_twh": {str(int(y)): round(float(v), 4) for y, v in res.items()},
           "model_fingerprint": meta["model_fingerprint"], "map_meta_sha256": rs.file_sha256(rs.META_PATH),
           "year_to_draw": {str(y): d for y, d in sorted(sel.items())},
           "workbook_sha256": prov["workbook_sha256"], "fr_sheets_sha256": prov["fr_sheets_sha256"],
           "frame_keys_sha256": prov["frame_keys_sha256"],
           "measured_utc": datetime.now(UTC).isoformat(timespec="seconds")}
    for y in sorted(sel):
        print(f"  {y}  draw {sel[y]:2d}  demand {ann[y]:7.1f} TWh  RES {res[y]:6.1f} TWh")
    # UNCHANGED MEASUREMENT, UNCHANGED FILE. make_sensitivities stamps this file's sha256, and a new timestamp alone
    # would rebuild every variant workbook -- whose new bytes move every finished variant to `_stale/`.
    if out.exists():
        was = json.loads(out.read_text(encoding="utf-8"))
        if {k: v for k, v in was.items() if k != "measured_utc"} == {k: v for k, v in rec.items()
                                                                     if k != "measured_utc"}:
            print(f"[ref] {out} already holds this measurement - left unchanged")
            return was
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    os.replace(tmp, out)
    print(f"[ref] wrote {out}")
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--frames-only", action="store_true", help="snapshot the drivers and compute the frames, then exit")
    ap.add_argument("--measure-demand", action="store_true", help="write the FR demand reference, then exit")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--years", default=None)
    ap.add_argument("--ref-year", type=int, default=2019)
    ap.add_argument("--draw", type=int, default=rs.REF_DRAW)
    ap.add_argument("--out", default=None)
    ap.add_argument("--n-weeks", type=int, default=None)
    ap.add_argument("--allow-env", default="",
                    help="comma-separated DISPATCH_*/POWERSIM_* switches that are this variant's own change")
    a = ap.parse_args(argv)
    if a.draw != rs.REF_DRAW:
        raise SystemExit(f"--draw must be {rs.REF_DRAW}: the stitched availability carries that label only")
    out = Path(a.out).resolve() if a.out else None
    os.chdir(rs.REPO)

    sel, meta = rs.load_map()
    _set_env("POWERSIM_RES_OFFSHORE_LOSSES", "1", lambda c, v: c.strip().lower() in ("1", "true", "yes"))
    if a.measure_demand:
        os.environ.pop("POWERSIM_SCENARIOS", None)
    stitched, prov = reference_frame(sel, meta)
    print(f"[ref] map {len(sel)} years from {len(set(sel.values()))} draws of {meta['model_fingerprint']}; "
          f"workbook {Path(prov['workbook']).name} (ensemble's: {prov['reproduces_ensemble_workbook']})", flush=True)
    if a.measure_demand:
        measure_demand(sel, meta, stitched, prov)
        return 0
    if a.frames_only:
        return 0
    if out is None:
        raise SystemExit("--out is required for a run")
    if os.environ.get("PYTHONHASHSEED", "random") in ("", "random"):
        raise SystemExit("[ref] PYTHONHASHSEED is not pinned: every process would solve a permuted LP (module "
                         "docstring); set it before starting python (run_sensitivities sets 0)")

    _set_env("DISPATCH_AVAIL_WDRAW", str(rs.AVAIL_WDRAW))
    _set_env("POWERSIM_WEATHER_CUBE", str(rs.CUBE_PATH), _same_path)
    problems = rs.verify_cube(sel, meta) + rs.verify_availability(sel, meta)
    if problems:
        raise SystemExit("[ref] reference artefacts inconsistent with the map: " + "; ".join(problems))
    allowed = {k.strip() for k in a.allow_env.split(",") if k.strip()}
    drift = env_drift(ensemble_components(meta["model_fingerprint"]), allowed)
    if drift:
        raise SystemExit(f"[ref] environment switches {drift} differ from the ensemble's and are not declared as the "
                         f"variant's own (--allow-env): the run would not be the reference")

    from dispatch_model.config import load_config
    cfg = load_config(a.config)
    if not cfg.section("projection").get("weather_coherent", True):
        raise SystemExit("[ref] projection.weather_coherent is off: the stitched FR frame would not be used")
    hz = cfg.section("projection").get("horizon", {})
    years = ([int(y) for y in a.years.replace(" ", "").split(",") if y] if a.years else
             list(range(a.start or int(hz.get("start_year", 2027)), (a.end or int(hz.get("end_year", 2046))) + 1)))
    missing = sorted(set(years) - set(sel))
    if missing:
        raise SystemExit(f"[ref] years {missing} are not in the reference map")

    ident = run_identity(sel, meta, prov)
    out.mkdir(parents=True, exist_ok=True)
    rec_f = out / "reference_run.json"
    if any(out.glob("prices_*.parquet")):
        # run_projection_20y resumes on any year it finds: only years of THIS reference run may be resumed
        was = json.loads(rec_f.read_text(encoding="utf-8")).get("identity") if rec_f.exists() else None
        if was != ident:
            why = "reference_run.json differs" if was else "no reference_run.json"
            raise SystemExit(f"[ref] {out} holds years of another run ({why}): use a new --out or move it aside")
    tmp = rec_f.with_name(f".{rec_f.name}.{os.getpid()}.tmp")       # per process: the baseline runs several
    tmp.write_text(json.dumps({"identity": ident, "provenance": prov,
                               "started_utc": datetime.now(UTC).isoformat(timespec="seconds")}, indent=1),
                   encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, rec_f)
            break
        except PermissionError:                         # another process of this run is reading or replacing it
            if attempt == 9:
                raise
            time.sleep(2)

    install_strict_hooks(cfg)
    from dispatch_model import weather_shapes as wsh
    wsh._DRAW_CACHE[rs.REF_DRAW] = stitched

    import run_projection_20y
    fwd = ["--config", a.config, "--ref-year", str(a.ref_year), "--draw", str(rs.REF_DRAW), "--out", str(out)]
    fwd += ["--years", ",".join(map(str, years))] if a.years else ["--start", str(years[0]), "--end", str(years[-1])]
    fwd += ["--n-weeks", str(a.n_weeks)] if a.n_weeks else []
    sys.argv = [str(_HERE / "run_projection_20y.py"), *fwd]
    return run_projection_20y.main()


if __name__ == "__main__":
    raise SystemExit(main())
