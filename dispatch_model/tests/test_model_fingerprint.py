"""The fingerprint must move when the model moves, and the marker must not be trusted across models.

These are regression tests for a failure that has now happened twice: a new ensemble reusing the price
parquets of an old one because a directory name matched. See `scripts/model_fingerprint.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import model_fingerprint as mf  # noqa: E402


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    """A miniature repo: one package file, one script, one config, one workbook."""
    (tmp_path / "pkg" / "pkg").mkdir(parents=True)
    (tmp_path / "pkg" / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "pkg" / "__pycache__" / "a.py").write_text("IGNORED\n", encoding="utf-8")
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "run.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "c.yaml").write_text("k: v\n", encoding="utf-8")
    (tmp_path / "scenarios.xlsx").write_bytes(b"workbook-bytes")
    monkeypatch.setattr(mf, "_ROOT", tmp_path)
    monkeypatch.setattr(mf, "_PACKAGES", ("pkg/pkg",))
    monkeypatch.setattr(mf, "_SCRIPTS", ("s/run.py",))
    monkeypatch.setattr(mf, "_CONFIGS", ("c.yaml",))
    return tmp_path


def test_digest_is_stable(tree):
    assert mf.digest() == mf.digest()


def test_pycache_is_not_hashed(tree):
    assert "pkg/pkg/__pycache__/a.py" not in mf.components()


@pytest.mark.parametrize("rel", ["pkg/pkg/a.py", "s/run.py", "c.yaml", "scenarios.xlsx"])
def test_digest_moves_when_any_input_moves(tree, rel):
    """Model code, run script, config and assumptions each decide a price, so each must move the digest."""
    before = mf.digest()
    p = tree / rel
    p.write_bytes(p.read_bytes() + b"# changed\n")
    assert mf.digest() != before, f"changing {rel} left the fingerprint unchanged"


def test_digest_moves_when_a_package_file_is_added_or_removed(tree):
    before = mf.digest()
    (tree / "pkg" / "pkg" / "b.py").write_text("z = 3\n", encoding="utf-8")
    added = mf.digest()
    assert added != before
    (tree / "pkg" / "pkg" / "b.py").unlink()
    assert mf.digest() == before


def test_a_missing_package_is_recorded_not_skipped(tree, monkeypatch):
    """A tree that vanishes must move the fingerprint. Skipping it silently would make a truncated
    checkout hash the same as a complete one."""
    monkeypatch.setattr(mf, "_PACKAGES", ("pkg/pkg", "gone/gone"))
    assert mf.components()["gone/gone"] == "ABSENT"


def test_scalars_change_the_digest(tree):
    assert mf.digest({"master_seed": 1}) != mf.digest({"master_seed": 2})
    assert mf.digest({"n_weeks": None}) != mf.digest({"n_weeks": 4})


def test_stamp_writes_a_diffable_component_map(tree, tmp_path):
    a = tmp_path / "wa"
    fp_a = mf.stamp(a)
    assert fp_a == mf.digest()
    (tree / "pkg" / "pkg" / "a.py").write_text("x = 99\n", encoding="utf-8")
    b = tmp_path / "wb"
    fp_b = mf.stamp(b)
    assert fp_b != fp_a
    # a bare hash says "something moved"; the component map has to say WHICH file
    assert mf.diff(a / "FINGERPRINT.json", b / "FINGERPRINT.json") == ["pkg/pkg/a.py"]


# --- the marker, one level up ---------------------------------------------------------------------------

def test_draw_marker_is_not_trusted_across_models(tmp_path, monkeypatch, capsys):
    """A `.draw_complete` left by another model must NOT skip the draw. This is the harsher half of the
    bug: an unfingerprinted marker skips before the fingerprinted work directory is ever consulted."""
    import run_montecarlo as rm

    ddir = tmp_path / "draw_007"
    ddir.mkdir()
    (ddir / ".draw_complete").write_text("OLDMODEL1234", encoding="utf-8")
    monkeypatch.chdir(tmp_path)                                           # run_draw uses relative scratchpad/ paths
    monkeypatch.setattr(mf, "digest", lambda extra=None, n=12: "NEWMODEL5678")   # the model did not change mid-run

    class _ReachedError(RuntimeError):
        pass

    # the first thing run_draw does after the marker gate is import mc_weather; make that the tripwire
    monkeypatch.setitem(sys.modules, "mc_weather",
                        type(sys)("mc_weather"))
    sys.modules["mc_weather"].generate_cube = lambda *a, **k: (_ for _ in ()).throw(_ReachedError())
    sys.modules["mc_weather"].weather_summary = lambda *a, **k: None

    with pytest.raises(_ReachedError):
        rm.run_draw(7, tmp_path, master_seed=1, keep_cube=False, fp="NEWMODEL5678")
    assert "RECOMPUTING" in capsys.readouterr().out


def test_draw_marker_is_trusted_within_one_model(tmp_path, monkeypatch, capsys):
    import run_montecarlo as rm

    ddir = tmp_path / "draw_007"
    ddir.mkdir()
    (ddir / ".draw_complete").write_text("SAMEMODEL999", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mf, "digest", lambda extra=None, n=12: "SAMEMODEL999")
    rm.run_draw(7, tmp_path, master_seed=1, keep_cube=False, fp="SAMEMODEL999")   # must not raise
    assert "already complete" in capsys.readouterr().out


def test_the_deliverables_builder_is_not_part_of_the_solve_digest():
    """It cannot change a solved year, so a fix to it must not rename the work directory and re-solve an ensemble."""
    import model_fingerprint as real
    assert not any("build_projection_deliverables" in s for s in real._SCRIPTS)
    assert "dispatch_model/scripts/build_projection_deliverables.py" in real.NON_SOLVE
    assert not any("build_projection_deliverables" in k for k in real.components())


def test_fitted_artefacts_and_the_weather_generator_are_part_of_the_digest():
    """A refit changes only parameter files; if they are not hashed, a refit model reuses the old model's years."""
    import model_fingerprint as real
    if not (real._ROOT / "weathergen" / "models" / "wind100.json").exists():
        pytest.skip("fitted artefacts are gitignored and absent from this checkout")
    keys = real.components()
    assert any(k.startswith("weathergen/weathergen/") for k in keys)
    assert "weathergen/config.yaml" in keys
    assert "weathergen/models/wind100.json" in keys
    assert any(k.startswith("res_model/models/") for k in keys)
    assert "dispatch_model/reports/markup_model.json" in keys


def test_a_large_npz_is_sampled_and_a_change_to_a_middle_member_still_moves_the_digest(tree, monkeypatch):
    """The sampling claim itself: np.savez writes a zip whose central directory, at the tail, carries every
    member's CRC32, so a same-size edit deep inside a middle member moves the sampled hash."""
    import numpy as np
    monkeypatch.setattr(mf, "_SAMPLE_ABOVE", 64 * 1024)
    monkeypatch.setattr(mf, "_SAMPLE", 16 * 1024)
    monkeypatch.setattr(mf, "_ARTEFACTS", ("art/*.npz",))
    (tree / "art").mkdir()
    f = tree / "art" / "big.npz"
    arrays = {f"a{i}": np.zeros(100_000) for i in range(10)}
    np.savez(f, **arrays)
    before = mf.digest()
    assert mf.components()["art/big.npz"].startswith("sampled:")
    arrays["a5"][50_000] = 1.0
    np.savez(f, **arrays)
    assert f.stat().st_size == (tree / "art" / "big.npz").stat().st_size
    assert mf.digest() != before


def test_a_large_file_that_is_not_a_zip_is_hashed_in_full(tree, monkeypatch):
    monkeypatch.setattr(mf, "_SAMPLE_ABOVE", 1024)
    monkeypatch.setattr(mf, "_SAMPLE", 64)
    monkeypatch.setattr(mf, "_ARTEFACTS", ("art/*.bin",))
    (tree / "art").mkdir()
    f = tree / "art" / "big.bin"
    raw = bytearray(8192)
    f.write_bytes(bytes(raw))
    before = mf.digest()
    assert not mf.components()["art/big.bin"].startswith("sampled:")
    raw[4096] = 1                                                        # invisible to head/tail sampling
    f.write_bytes(bytes(raw))
    assert mf.digest() != before


def test_behaviour_switches_in_the_environment_move_the_digest_and_per_draw_values_do_not(tree, monkeypatch):
    monkeypatch.delenv("DISPATCH_TEST_SWITCH", raising=False)
    before = mf.digest()
    monkeypatch.setenv("DISPATCH_TEST_SWITCH", "1")
    switched = mf.digest()
    assert switched != before
    monkeypatch.setenv("POWERSIM_WEATHER_CUBE", "/draws/a.nc")
    a = mf.digest()
    monkeypatch.setenv("POWERSIM_WEATHER_CUBE", "/draws/b.nc")
    assert mf.digest() == a


def test_the_database_is_identified_by_size_and_modification_time(tree, monkeypatch):
    import os
    monkeypatch.setattr(mf, "_DATABASES", ("db/x.db",))
    (tree / "db").mkdir()
    f = tree / "db" / "x.db"
    f.write_bytes(b"sqlite")
    before = mf.digest()
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    assert mf.digest() != before
