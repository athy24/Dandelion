"""Scenario workbook accessor (§7, ADR-5).

One `scenarios.xlsx` at the repo root is the single hand-edited source of assumptions. Its tabs are
prefixed by model — `avail_fleet_registry`, `demand_macro`, `res_capacity_trajectories`,
`dispatch_commodities`, … — so names stay unique and it is obvious what belongs where. This module is the
one read path: `load_model_sheets(path, "avail")` returns that model's tabs with the prefix stripped, so
each model sees its historical sheet names unchanged.

`snapshot()` freezes the workbook to immutable Parquet + a manifest (source hash + timestamp) for
provenance — you can always tell exactly which assumption values produced a given run. Editing stays in
the one xlsx; the snapshot is a code-side record, invisible to the editor.

`POWERSIM_SCENARIOS` redirects that one file for a whole process — see `resolve()`.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from . import lake

MODEL_PREFIXES = ("avail_", "demand_", "res_", "dispatch_")


def _sep(prefix: str) -> str:
    return f"{prefix}_"


def resolve(path: str | Path) -> Path:
    """The workbook actually read: `path`, unless `POWERSIM_SCENARIOS` names another file.

    A SENSITIVITY is one changed assumption against an otherwise identical reference, and the four models
    (iii demand, iv RES, v availability, vi/vii dispatch) each resolve `../scenarios.xlsx` from their own
    config. Editing the shared file would make every concurrent run see the change and would leave no
    reference to compare against; four config edits per variant would be four chances to move only three.
    One environment variable moves all four at once, and only for the process that sets it — so variants
    run side by side, each on its own workbook, with the reference file untouched.

    Read at call time, never cached at import: the pool workers of `run_sensitivities.py` each set their
    own value, and a module-level constant would freeze whichever variant imported first.

    A named-but-missing file is an ERROR, never a silent fallback to the reference — a sensitivity that
    quietly reruns the base case is indistinguishable from one that legitimately moved nothing.
    """
    override = os.environ.get("POWERSIM_SCENARIOS")
    if not override:
        return Path(path)
    p = Path(override)
    if not p.exists():
        raise FileNotFoundError(f"POWERSIM_SCENARIOS={override} does not exist")
    return p


def load_model_sheets(path: str | Path, prefix: str) -> dict[str, pd.DataFrame]:
    """This model's tabs from the merged scenarios.xlsx, keyed with the `{prefix}_` stripped.

    Tolerant of a **legacy single-model workbook** (unprefixed tabs): if the file carries no model-prefixed
    tabs at all, every tab is returned as-is. But a merged workbook that simply lacks this model's tabs is
    an error (misconfiguration), not a silent all-tabs fallback.
    """
    pre = _sep(prefix)
    path = resolve(path)
    xl = pd.read_excel(path, sheet_name=None)
    out = {name[len(pre):]: df for name, df in xl.items() if name.startswith(pre)}
    if out:
        return out
    if any(name.startswith(k) for name in xl for k in MODEL_PREFIXES):
        raise ValueError(f"no '{pre}*' tabs in {path} (merged workbook missing this model?)")
    return dict(xl)                                          # unprefixed single-model / template workbook


def load_sheet(path: str | Path, prefix: str, sheet: str) -> pd.DataFrame:
    """One tab, addressed by (prefix, unprefixed sheet name)."""
    return pd.read_excel(resolve(path), sheet_name=f"{_sep(prefix)}{sheet}")


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(path: str | Path, out_dir: str | Path | None = None) -> Path:
    """Freeze every tab of the workbook to Parquet + a manifest (source hash, timestamp, per-tab shape).

    Immutable provenance record under `data/lake/scenarios/<sha12>/`. Returns the manifest path.
    """
    path = resolve(path)                                     # freeze what is READ, not what was asked for
    digest = file_sha256(path)
    out_dir = Path(out_dir) if out_dir else (lake.lake_root() / "scenarios" / digest[:12])
    out_dir.mkdir(parents=True, exist_ok=True)
    xl = pd.read_excel(path, sheet_name=None)
    tabs = {}
    for name, df in xl.items():
        df.to_parquet(out_dir / f"{name}.parquet", compression="zstd", index=False)
        tabs[name] = list(df.shape)
    manifest = {"source": str(path), "sha256": digest, "frozen_at_utc": datetime.now(UTC).isoformat(),
                "tabs": tabs}
    mpath = out_dir / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return mpath
