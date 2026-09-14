"""A sensitivity counts only under the model and reference weather that produced it -- for the runner AND for every
reader.

Regression tests for what the 2026-09-14 review of the wind100 refit found: consolidation and the overlay charts
counted any `.sens_complete`, so a relaunch would have published deltas between a new-model baseline and old-model
variants until each stale variant's slot came round. See scripts/run_sensitivities.py (variant_fingerprints,
complete_variants).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import model_fingerprint as mf  # noqa: E402
import run_sensitivities as rs  # noqa: E402


def test_only_a_marker_naming_the_variants_own_fingerprint_counts(tmp_path):
    fps = {"s00": "AAA000000000", "s01": "BBB000000000", "s02": "CCC000000000", "s03": "DDD000000000"}
    for vid, text in (("s00", "AAA000000000"),                 # this model
                      ("s01", ""),                             # written before markers carried a fingerprint
                      ("s02", "OLDMODEL0000")):                # another model
        (tmp_path / vid).mkdir()
        (tmp_path / vid / ".sens_complete").write_text(text, encoding="utf-8")
    assert rs.complete_variants(tmp_path, fps) == {"s00": tmp_path / "s00"}


def test_a_variants_fingerprint_moves_with_the_model_the_reference_cube_and_its_workbook(tmp_path, monkeypatch):
    cube = tmp_path / "ref.nc"
    cube.write_bytes(b"cube")
    wb = tmp_path / "wb.xlsx"
    wb.write_bytes(b"workbook")
    monkeypatch.setattr(rs, "CUBE", cube)
    model = {"id": "MODEL_A"}
    monkeypatch.setattr(mf, "digest", lambda extra=None, n=12: model["id"] + json.dumps(extra, sort_keys=True))
    variants = [{"id": "s00"}, {"id": "s01", "workbook": str(wb)}, {"id": "s02", "env": {"DISPATCH_X": "1"}}]

    base = rs.variant_fingerprints(variants, 2027, 2046, None)
    assert len(set(base.values())) == 3
    assert rs.variant_fingerprints(variants, 2027, 2046, None) == base

    model["id"] = "MODEL_B"
    changed = rs.variant_fingerprints(variants, 2027, 2046, None)
    assert all(changed[v] != base[v] for v in base)
    model["id"] = "MODEL_A"

    st = cube.stat()
    os.utime(cube, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))       # another reference weather
    moved = rs.variant_fingerprints(variants, 2027, 2046, None)
    assert all(moved[v] != base[v] for v in base)

    wb.write_bytes(b"workbook, edited")
    assert rs.variant_fingerprints(variants, 2027, 2046, None)["s01"] != moved["s01"]
