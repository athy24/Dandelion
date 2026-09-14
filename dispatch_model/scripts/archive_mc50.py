"""Archive the completed 50-draw ensemble, with the provenance needed to trust it later.

Two archives, because one artefact cannot serve both purposes:

  * FULL      every file of every draw, ~1.6 GB raw. The record of what was actually computed.
  * SUMMARY   everything EXCEPT `hourly_spot_prices.csv` and `projection_20y_spot_prices.xlsx`, which are
              30.8 of each draw's 31.4 MB. What remains is every aggregate anyone reads -- price summary,
              revenue by tech, capture prices, congestion rents, adequacy, the analysis workbook and the
              draw's own weather -- at ~0.6 MB per draw, small enough to send by mail.

Both carry a MANIFEST with a SHA-256 per file, the draw count, and the git commit the ensemble was
computed at. A Monte-Carlo whose provenance is a directory listing is not reproducible; one that names its
commit and hashes its contents is.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]          # ...\PriceModeling\dispatch_model
SRC = REPO / "reports" / "mc50"
OUT = REPO.parent / "archive"
OUT.mkdir(parents=True, exist_ok=True)
STAMP = datetime.now(UTC).strftime("%Y%m%d")

#: excluded from the SUMMARY archive only -- the hourly series, which dominate the size
BULKY = {"hourly_spot_prices.csv", "projection_20y_spot_prices.xlsx"}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                        # noqa: BLE001
        return "unknown"


# ONLY DRAWS OF ONE MODEL. A `.draw_complete` carries the fingerprint of the model that wrote it; counting any
# marker would merge two models into one "ensemble" whenever a recompute is under way.
import argparse  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_fingerprint import digest  # noqa: E402

_ap = argparse.ArgumentParser()
_ap.add_argument("--fingerprint", default=None,
                 help="only draws whose marker names this model (default: the model on disk now)")
_args = _ap.parse_args()
FP = _args.fingerprint or digest({"master_seed": 20260629, "n_weeks": None})             # run_montecarlo defaults
_marked = sorted(d for d in SRC.glob("draw_*") if (d / ".draw_complete").exists())
draws = [d for d in _marked if (d / ".draw_complete").read_text(encoding="utf-8").strip() == FP]
if len(draws) < len(_marked):
    print(f"  {len(_marked) - len(draws)} completed draw(s) belong to another model and are left out")
if not draws:
    raise SystemExit(f"no completed draw of model {FP} in {SRC}")
print(f"draws complete for model {FP}: {len(draws)}")
if len(draws) != 50:
    print(f"WARNING: expected 50, found {len(draws)} -- archiving what exists")

# only the selected draws' files: every file under reports/mc50 would sweep in draws of another model
files = sorted(f for d in draws for f in d.rglob("*") if f.is_file())
print(f"files: {len(files)}   raw size: {sum(f.stat().st_size for f in files)/1e6:.0f} MB")
print("hashing...", flush=True)
digests = {str(f.relative_to(SRC)).replace("\\", "/"): {"sha256": sha256(f), "bytes": f.stat().st_size}
           for f in files}

manifest = {
    "ensemble": "mc50",
    "model_fingerprint": FP,
    "draws": [int(d.name.split("_")[1]) for d in draws],
    "n_draws": len(draws),
    "archived_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    "git_commit": git("rev-parse", "HEAD"),
    "git_describe": git("log", "-1", "--format=%h %s"),
    "git_dirty": bool(git("status", "--porcelain")),
    "master_seed": 20260629,
    "workers": 3,
    "note": ("50 coherent weather draws through steps iii-vii. Each draw: its own weathergen cube, its own "
             "step-v availability (layer availability_mc, partition wdraw=d), its own demand/RES driver "
             "cache, its own 20-year dispatch. Draws 0 and 1 were recomputed after an earlier run reused "
             "stale year parquets; see git history."),
    "files": digests,
}
(OUT / f"mc50_manifest_{STAMP}.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
print(f"manifest -> mc50_manifest_{STAMP}.json")

for tag, keep in (("summary", lambda f: f.name not in BULKY), ("full", lambda f: True)):
    zp = OUT / f"mc50_{tag}_{STAMP}.zip"
    sel = [f for f in files if keep(f)]
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sel:
            z.write(f, str(Path("mc50") / f.relative_to(SRC)))
        z.writestr("mc50/MANIFEST.json", json.dumps(manifest, indent=1))
    print(f"{tag:8s}: {len(sel):4d} files -> {zp.name}  {zp.stat().st_size/1e6:7.1f} MB")

ref = OUT / f"REFERENCE_SCENARIOS_{STAMP}.xlsx"
src_wb = REPO.parent / "scenarios.xlsx"
ref.write_bytes(src_wb.read_bytes())
print(f"REFERENCE_SCENARIOS -> {ref.name}  {ref.stat().st_size/1e6:.2f} MB  sha256 {sha256(ref)[:16]}...")
