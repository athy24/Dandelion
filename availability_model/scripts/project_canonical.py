"""Produce the CANONICAL availability layer deliberately, and record what produced it.

The canonical `availability` layer is what the dispatch reads when `DISPATCH_AVAIL_WDRAW` is unset — every
single-run projection, every `--no-avail-weather` run, and the four artefacts `tools/golden.py` fingerprints.

It had no provenance. `availability_model/tests/test_projection.py` called `pipeline.project(cfg, n_draws=2)`,
which defaults to `layer="availability"`, so the canonical layer was simply whatever a two-draw test fixture
last wrote, on whatever weather cube happened to be configured when someone ran pytest. Its mtimes moved
twice on 2026-09-09 (09:44 and 10:12) purely from test runs. A test suite must not be the producer of a
production artefact, and a production artefact must be able to say where it came from.

The suite is now isolated (a session fixture redirects `POWERSIM_LAKE` to a tmp dir), so this script is the
only thing that writes the layer. It stamps `data/lake/availability/PROVENANCE.json` with the cube, the
realization, the draw count, the fleet-registry digest and the git commit, so the next reader can tell
whether the layer matches the code and the assumptions in front of them.

    python -u -X utf8 -W ignore scripts/project_canonical.py [--draws N] [--realization R]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from availability_model.config import load_config  # noqa: E402
from availability_model.projection.engine import load_scenario_registry, project  # noqa: E402


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                                   # noqa: BLE001
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=None, help="default: projection.n_draws from the config")
    ap.add_argument("--realization", type=int, default=0)
    a = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    cfg = load_config(str(root / "config.yaml"))
    import os
    cube = os.environ.get("POWERSIM_WEATHER_CUBE") or str(
        cfg.resolve(cfg.section("data")["weathergen_output"]))
    n = int(a.draws or cfg.section("projection")["n_draws"])
    reg = load_scenario_registry(cfg)
    nuc = reg[reg["technology"] == "nuclear"]

    print(f"cube        : {cube}")
    print(f"draws       : {n}   realization: {a.realization}")
    print(f"registry    : {len(reg)} units, nuclear {len(nuc)} / {nuc['capacity_mw'].sum() / 1e3:.2f} GW, "
          f"largest {nuc['capacity_mw'].max():.0f} MW")
    if nuc["capacity_mw"].max() > 2000:
        raise SystemExit("registry still carries an implausible nuclear capacity — repair it first "
                         "(scripts/repair_fleet_registry.py)")

    by_tech = project(cfg, n_draws=n, realization=a.realization)

    from powersim_core import lake
    out = lake.dataset_dir("availability", "availability_by_tech").parent
    digest = hashlib.sha256(
        pd.util.hash_pandas_object(reg.astype(str), index=False).values.tobytes()).hexdigest()
    prov = {
        "layer": "availability",
        "written_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "written_by": "availability_model/scripts/project_canonical.py",
        "weather_cube": cube,
        "realization": int(a.realization),
        "n_draws": n,
        "horizon": cfg.section("projection")["horizon"],
        "registry_units": int(len(reg)),
        "registry_nuclear_gw": round(float(nuc["capacity_mw"].sum()) / 1e3, 3),
        "registry_sha256": digest,
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "note": ("Deliberate projection. Before 2026-09-09 this layer was produced as a side effect of "
                 "availability_model/tests/test_projection.py, which called project(n_draws=2) with the "
                 "default layer; the suite is now isolated with POWERSIM_LAKE."),
    }
    (out / "PROVENANCE.json").write_text(json.dumps(prov, indent=1), encoding="utf-8")
    print(f"\nwrote {out / 'PROVENANCE.json'}")

    nb = by_tech[by_tech["technology"] == "nuclear"] if by_tech is not None else None
    if nb is not None and "installed_mw" in nb.columns:
        print(f"nuclear installed {nb['installed_mw'].max():.0f} MW, "
              f"mean availability {(nb['available_mw'] / nb['installed_mw']).mean():.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
