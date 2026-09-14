"""A relaunch must not reuse what another model produced — cubes, markers — nor run under a model that changed.

Regression tests for what the 2026-09-14 review of the wind100 refit found: three draw cubes left by an interrupted
run would have been reused under the new digest, a recomputing draw kept its old marker until it finished, and the
digest was computed once for a multi-day run. See scripts/run_montecarlo.py (run_draw).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import model_fingerprint as mf  # noqa: E402
import run_montecarlo as rm  # noqa: E402


class _TripwireError(RuntimeError):
    pass


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """run_draw works in relative scratchpad/ paths: run it in tmp, with the weather generator and every
    subprocess replaced by tripwires that record the call."""
    monkeypatch.chdir(tmp_path)
    calls = {"generate": 0, "run": 0}
    fake = type(sys)("mc_weather")

    def _generate(draw, out_path, master_seed=0, model_fp=None):
        calls["generate"] += 1
        raise _TripwireError("generate_cube")

    fake.generate_cube = _generate
    fake.weather_summary = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "mc_weather", fake)

    def _run(cmd, env, label):
        calls["run"] += 1
        raise _TripwireError(label)

    monkeypatch.setattr(rm, "_run", _run)
    return tmp_path, calls


def _cube(path: Path, **attrs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    xr.Dataset({"obs": ("t", np.zeros(3))}, attrs=attrs).to_netcdf(path)


def test_a_draw_refuses_to_run_when_the_model_changed_under_the_ensemble(sandbox, monkeypatch):
    root, calls = sandbox
    monkeypatch.setattr(mf, "digest", lambda extra=None, n=12: "CHANGED00000")
    with pytest.raises(RuntimeError, match="changed under the running ensemble"):
        rm.run_draw(3, root / "reports", master_seed=1, keep_cube=False, fp="LAUNCHED0000")
    assert calls == {"generate": 0, "run": 0}


def test_a_stale_marker_is_retired_before_the_draw_recomputes(sandbox, monkeypatch):
    root, _ = sandbox
    monkeypatch.setattr(mf, "digest", lambda extra=None, n=12: "NEWMODEL0000")
    ddir = root / "reports" / "draw_004"
    ddir.mkdir(parents=True)
    (ddir / ".draw_complete").write_text("OLDMODEL0000", encoding="utf-8")
    (ddir / ".deliverables_fingerprint").write_text("oldbuilder", encoding="utf-8")
    with pytest.raises(_TripwireError):
        rm.run_draw(4, root / "reports", master_seed=1, keep_cube=False, fp="NEWMODEL0000")
    assert not (ddir / ".draw_complete").exists()                   # gone before any work, not after it
    assert not (ddir / ".deliverables_fingerprint").exists()


def test_a_cube_left_by_another_model_is_regenerated(sandbox, monkeypatch):
    root, calls = sandbox
    monkeypatch.setattr(mf, "digest", lambda extra=None, n=12: "NEWMODEL0000")
    cube = root / "scratchpad" / "mc" / "reports-NEWMODEL0000" / "cube_005.nc"
    _cube(cube, model_fp="OLDMODEL0000", mc_master_seed=1, mc_draw=5)
    with pytest.raises(_TripwireError, match="generate_cube"):
        rm.run_draw(5, root / "reports", master_seed=1, keep_cube=False, fp="NEWMODEL0000")
    assert calls["generate"] == 1
    assert not cube.exists()


def test_a_cube_of_another_draw_is_regenerated(sandbox, monkeypatch):
    root, calls = sandbox
    monkeypatch.setattr(mf, "digest", lambda extra=None, n=12: "NEWMODEL0000")
    cube = root / "scratchpad" / "mc" / "reports-NEWMODEL0000" / "cube_007.nc"
    _cube(cube, model_fp="NEWMODEL0000", mc_master_seed=1, mc_draw=8)
    with pytest.raises(_TripwireError, match="generate_cube"):
        rm.run_draw(7, root / "reports", master_seed=1, keep_cube=False, fp="NEWMODEL0000")
    assert calls["generate"] == 1


def test_a_cube_of_this_model_seed_and_draw_is_reused(sandbox, monkeypatch):
    root, calls = sandbox
    monkeypatch.setattr(mf, "digest", lambda extra=None, n=12: "NEWMODEL0000")
    cube = root / "scratchpad" / "mc" / "reports-NEWMODEL0000" / "cube_006.nc"
    _cube(cube, model_fp="NEWMODEL0000", mc_master_seed=1, mc_draw=6)
    with pytest.raises(_TripwireError, match="step-v"):
        rm.run_draw(6, root / "reports", master_seed=1, keep_cube=False, fp="NEWMODEL0000")
    assert calls["generate"] == 0
    assert cube.exists()
