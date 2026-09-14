"""A digest of everything that decides a projected price, so cached years cannot outlive the model.

WHY THIS EXISTS. `run_projection_20y` skips any year whose `prices_<y>.parquet` already exists. That is
what lets an ensemble survive a reboot or an overnight kill, and it is worth keeping. But "the file exists"
is not the same question as "the file was produced by this model", and the two were being conflated.

Twice now the difference has cost an ensemble:

  * 2026-08: a fresh 50-draw run "completed" draws 0 and 1 in 0.4 minutes each, reusing parquets written
    twelve days earlier by a smoke run — code predating the run-of-river split, the feed-parity repairs and
    the GB capacity trajectory. The fix was to namespace the work directory on the output directory name.
  * 2026-09-09: the SAME failure, through the namespace. The four-defect availability repair was completed,
    `reports/mc50` and the fifty `availability_mc` partitions were deleted, and the ensemble relaunched
    into `reports/mc50` again. `scratchpad/mc/mc50` had not been deleted, so the name matched, and 41 of
    50 draws silently reused pre-repair prices. The ensemble was a 9/41 mixture of two different models,
    and the horizon mean differed by 2.3 EUR/MWh between the halves (12.1 over 2043-46). Every marker,
    every progress line and every deliverable said 50/50.

A NAME IS NOT A FINGERPRINT. The 2026-08 fix asked the operator to remember to use a new name; the
2026-09 failure is what that request is worth. This module answers the question directly: hash the code,
the assumptions and the seeds, and key the work directory on the answer. A cached year is then reused if
and only if it was produced by the same model, and no one has to remember anything.

WHAT IS HASHED, and why each part:

  * every `*.py` in the model packages, the weather generator and `powersim_core` — the model itself;
  * every FITTED ARTEFACT the models load at runtime: `*/models/*.json|npz` and the step-vii markup. A refit
    changes nothing but these files. On 2026-09-14 the 100 m wind co-generation was refit for its diurnal
    cycle; had only code been hashed, every cached year solved on the old wind field would have been reused.
    Files above 256 MB (the 2.8 GB `weathergen/models/fitted.json.npz`) are hashed on size, head and tail:
    an npz is a zip, whose central directory at the tail carries every member's CRC32, so any member that
    changes moves the tail. Only zips are sampled; any other large file is hashed in full;
  * the SQLite database every model reads (18 GB), identified by size and modification time rather than
    content. Nothing on the solve path writes it (ETL and ERA5 ingestion only), so it cannot move under a run,
    and any ingestion does move it;
  * every `DISPATCH_*` / `POWERSIM_*` environment variable that is SET — about twenty behaviour switches are read
    on the solve path — except the per-draw values run_montecarlo sets itself, and POWERSIM_SCENARIOS, whose
    effect the resolved workbook's bytes already carry;
  * the versions of the numerical libraries and of Python: a HiGHS upgrade can change which vertex a degenerate
    LP returns, hence the duals, hence the prices;
  * the three `scripts/*.py` that carry a draw through the SOLVE, INCLUDING this file's caller. A change to
    the orchestration (which cube, which seed, which env) changes prices as surely as a change to the LP.
    The deliverables builder is deliberately NOT one of them -- see `NON_SOLVE`;
  * every `config.yaml` — horizons, calibration, solver settings;
  * the resolved scenario workbook, byte for byte — the assumptions;
  * the scalars passed in: master seed, and `n_weeks` when a run is truncated.

WHAT IS DELIBERATELY NOT HASHED: the weather cube. For the Monte-Carlo it does not exist yet — it is
generated per draw from `SeedSequence([master_seed, draw])`, so the seed already covers it. A caller
running against a FIXED cube (the sensitivities, on REFERENCE_WEATHER_20Y) must pass it through `extra`,
which is what that argument is for; the cube is identified by path, size and mtime rather than hashed,
because hashing 472 MB per draw would cost more than it saves.

The components dict is written beside the digest so two fingerprints can be DIFFED. A bare hash tells you
that something moved; `FINGERPRINT.json` tells you which file, which is the difference between a five-second
diagnosis and an afternoon.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]

#: package trees whose every `*.py` decides a price
_PACKAGES = (
    "dispatch_model/dispatch_model",
    "demand_model/demand_model",
    "res_model/res_model",
    "availability_model/availability_model",
    "weathergen/weathergen",
    "powersim_core",
)

#: the scripts that carry one draw from weather through the solve; a change here moves prices too
_SCRIPTS = (
    "dispatch_model/scripts/run_montecarlo.py",
    "dispatch_model/scripts/run_projection_20y.py",
    "dispatch_model/scripts/mc_weather.py",
)

#: Components that CANNOT change a solved year, and so are not part of the solve digest. The deliverables builder
#: reads the per-year parquets and formats them, in its own process, after the solve; nothing on the solve path
#: imports it. Hashing it meant a formatting fix renamed the work directory and re-solved the ensemble -- on
#: 2026-09-14 that would have been ~6 days to reproduce correct parquets, for an interleaved hourly export.
#: Deliverables carry their own `.deliverables_fingerprint` instead; `scripts/migrate_ensemble.py` rebuilds
#: stale ones and bridges this one-time change of digest for an ensemble already in flight.
NON_SOLVE = ("dispatch_model/scripts/build_projection_deliverables.py",)

_CONFIGS = (
    "dispatch_model/config.yaml",
    "demand_model/config.yaml",
    "res_model/config.yaml",
    "availability_model/config.yaml",
    "weathergen/config.yaml",
)

#: fitted parameters loaded at runtime (top level of each models dir only: backups live in sub-directories)
_ARTEFACTS = (
    "weathergen/models/*.json",
    "weathergen/models/*.npz",
    "res_model/models/*.json",
    "res_model/models/*.npz",
    "demand_model/models/*.json",
    "demand_model/models/*.npz",
    "availability_model/models/*.json",
    "availability_model/models/*.npz",
    "dispatch_model/reports/markup_model.json",
    "weathergen/output/zone_hist.parquet",        # observed per-zone weather the neighbour response is fitted on
)

_SAMPLE_ABOVE = 256 * 1024 * 1024
_SAMPLE = 16 * 1024 * 1024

#: identified by (size, mtime_ns), never content-hashed — see the module docstring
_DATABASES = ("data/pricemodeling.db",)
#: environment switches recorded when set; the per-draw ones identify a draw, not a model
_ENV_PREFIXES = ("DISPATCH_", "POWERSIM_")
_ENV_EXCLUDED = frozenset({"POWERSIM_WEATHER_CUBE", "DISPATCH_AVAIL_WDRAW", "DISPATCH_CAPTURE_DISPATCH",
                           "POWERSIM_SCENARIOS"})
#: libraries whose version can change a solved year
_LIBS = ("highspy", "numpy", "scipy", "pandas", "xarray", "netCDF4", "pyarrow")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    size = path.stat().st_size
    if size > _SAMPLE_ABOVE and zipfile.is_zipfile(path):  # see the module docstring
        with path.open("rb") as fh:
            h.update(str(size).encode("ascii"))
            h.update(fh.read(_SAMPLE))
            fh.seek(size - _SAMPLE)
            h.update(fh.read(_SAMPLE))
        return "sampled:" + h.hexdigest()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def components(extra: dict[str, object] | None = None) -> dict[str, str]:
    """Every input that decides a price, mapped to its digest. Missing files are recorded as absent
    rather than skipped — a package that disappears must move the fingerprint, not leave it unchanged."""
    out: dict[str, str] = {}
    for pkg in _PACKAGES:
        base = _ROOT / pkg
        if not base.is_dir():
            out[pkg] = "ABSENT"
            continue
        for f in sorted(base.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            out[f.relative_to(_ROOT).as_posix()] = _sha(f)
    for rel in _SCRIPTS + _CONFIGS:
        p = _ROOT / rel
        out[rel] = _sha(p) if p.is_file() else "ABSENT"
    for pattern in _ARTEFACTS:
        hits = sorted(q for q in _ROOT.glob(pattern) if q.is_file())
        if not hits:
            out[pattern] = "ABSENT"
        for q in hits:
            out[q.relative_to(_ROOT).as_posix()] = _sha(q)

    # the assumptions, through the same switch every model reads them by
    try:
        from powersim_core.scenario import resolve as _resolve
        wb = Path(_resolve(_ROOT / "scenarios.xlsx"))
        out[f"workbook:{wb.name}"] = _sha(wb) if wb.is_file() else "ABSENT"
    except Exception as exc:                                             # noqa: BLE001
        out["workbook:UNRESOLVED"] = f"{type(exc).__name__}"

    for rel in _DATABASES:
        q = _ROOT / rel
        if q.is_file():
            st = q.stat()
            out[rel] = f"stat:{st.st_size}:{st.st_mtime_ns}"
        else:
            out[rel] = "ABSENT"
    for k in sorted(os.environ):
        if k.startswith(_ENV_PREFIXES) and k not in _ENV_EXCLUDED:
            out[f"env:{k}"] = os.environ[k]
    from importlib import metadata as _md
    for lib in _LIBS:
        try:
            out[f"lib:{lib}"] = _md.version(lib)
        except _md.PackageNotFoundError:
            out[f"lib:{lib}"] = "ABSENT"
    out["lib:python"] = sys.version.split()[0]

    for k, v in sorted((extra or {}).items()):
        out[f"param:{k}"] = str(v)
    return out


def digest_of(comp: dict[str, str], n: int = 12) -> str:
    """The digest of an already-computed component map, so one map can name both a work directory and its
    FINGERPRINT.json (two `components()` calls could straddle a file change)."""
    blob = "\n".join(f"{k}={v}" for k, v in sorted(comp.items())).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:n]


def digest(extra: dict[str, object] | None = None, n: int = 12) -> str:
    """Short hex digest over :func:`components`. `n` characters is plenty to separate the handful of
    model states a project holds at once, and short enough to read in a directory name."""
    return digest_of(components(extra), n)


def stamp(work_root: Path, extra: dict[str, object] | None = None, comp: dict[str, str] | None = None) -> str:
    """Compute the digest, write `FINGERPRINT.json` beside the work directories, return the digest.

    The file carries the full component map, so `python -c "import json,sys; ..."` on two of them names
    the file that changed. It is rewritten on every call: the digest is in the directory name, so a
    rewrite can only ever restate what is already true of that directory."""
    comp = components(extra) if comp is None else comp
    d = digest_of(comp)
    work_root.mkdir(parents=True, exist_ok=True)
    (work_root / "FINGERPRINT.json").write_text(
        json.dumps({"digest": d, "n_components": len(comp), "components": comp}, indent=1),
        encoding="utf-8")
    return d


def diff(a: Path, b: Path) -> list[str]:
    """Component keys that differ between two `FINGERPRINT.json` files — the reason a cache was not reused."""
    ca = json.loads(Path(a).read_text(encoding="utf-8"))["components"]
    cb = json.loads(Path(b).read_text(encoding="utf-8"))["components"]
    return sorted(k for k in set(ca) | set(cb) if ca.get(k) != cb.get(k))


if __name__ == "__main__":                                               # `python scripts/model_fingerprint.py`
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) == 3:                                               # ... a/FINGERPRINT.json b/FINGERPRINT.json
        changed = diff(Path(sys.argv[1]), Path(sys.argv[2]))
        print(f"{len(changed)} component(s) differ:")
        for k in changed:
            print(f"    {k}")
    else:
        c = components()
        print(f"digest {digest()}  over {len(c)} components")
