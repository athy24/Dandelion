"""A digest change that cannot touch a solved year must not cost the ensemble its solved years -- and a change
that can, must not be bridged. See scripts/migrate_ensemble.py."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import migrate_ensemble as me  # noqa: E402

BKEY = "dispatch_model/scripts/build_projection_deliverables.py"
SOLVE = {"dispatch_model/dispatch_model/lp/a.py": "aaa", "workbook:scenarios.xlsx": "www",
         "param:master_seed": "1", "param:n_weeks": "None"}
OLD = "0123456789ab"


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(me, "REPO", tmp_path)
    monkeypatch.setattr(me.mf, "components", lambda extra=None: dict(SOLVE))
    monkeypatch.setattr(me, "solvers_alive", lambda: [])
    monkeypatch.setattr(me, "non_solve", lambda: (BKEY,))
    (tmp_path / "reports" / "mc50").mkdir(parents=True)
    return tmp_path


def _legacy(repo: Path, comp: dict, digest: str = OLD) -> Path:
    d = repo / "scratchpad" / "mc" / f"mc50-{digest}"
    (d / "proj_000").mkdir(parents=True)
    (d / "proj_000" / "prices_2027.parquet").write_bytes(b"cached year")
    (d / "FINGERPRINT.json").write_text(json.dumps({"digest": digest, "components": comp}), encoding="utf-8")
    return d


def _marker(repo: Path, n: int, value: str | None) -> Path:
    dd = repo / "reports" / "mc50" / f"draw_{n:03d}"
    dd.mkdir(parents=True, exist_ok=True)
    if value is not None:
        (dd / ".draw_complete").write_text(value, encoding="utf-8")
    return dd / ".draw_complete"


def _new_dir(repo: Path) -> Path:
    return repo / "scratchpad" / "mc" / f"mc50-{me.digest_of(SOLVE)}"


def test_migrates_when_only_the_builder_differs(repo):
    old = _legacy(repo, {**SOLVE, BKEY: "old-builder"})
    done, inflight, other = _marker(repo, 0, OLD), _marker(repo, 1, None), _marker(repo, 2, "ffffffffffff")
    assert me.migrate(repo / "reports" / "mc50", 1, None, rebuild_after=False) == 0
    new = _new_dir(repo)
    assert not old.exists() and new.is_dir()
    assert (new / "proj_000" / "prices_2027.parquet").read_bytes() == b"cached year"
    assert done.read_text(encoding="utf-8") == me.digest_of(SOLVE)
    assert not inflight.exists()
    assert other.read_text(encoding="utf-8") == "ffffffffffff"
    assert json.loads((new / "MIGRATED.json").read_text(encoding="utf-8"))["from"] == OLD


def test_refuses_when_a_solve_component_differs(repo):
    old = _legacy(repo, {**SOLVE, "dispatch_model/dispatch_model/lp/a.py": "CHANGED", BKEY: "old-builder"})
    m = _marker(repo, 0, OLD)
    assert me.migrate(repo / "reports" / "mc50", 1, None, rebuild_after=False) == 2
    assert old.is_dir() and not _new_dir(repo).exists()
    assert m.read_text(encoding="utf-8") == OLD


def test_refuses_while_solvers_are_alive(repo, monkeypatch):
    old = _legacy(repo, {**SOLVE, BKEY: "old-builder"})
    monkeypatch.setattr(me, "solvers_alive", lambda: [4242])
    assert me.migrate(repo / "reports" / "mc50", 1, None, rebuild_after=False) == 3
    assert old.is_dir() and not _new_dir(repo).exists()


def test_dry_run_changes_nothing(repo):
    old = _legacy(repo, {**SOLVE, BKEY: "old-builder"})
    m = _marker(repo, 0, OLD)
    assert me.migrate(repo / "reports" / "mc50", 1, None, dry=True) == 0
    assert old.is_dir() and not _new_dir(repo).exists()
    assert m.read_text(encoding="utf-8") == OLD


def test_noop_when_the_current_work_dir_exists(repo):
    new = _new_dir(repo)
    (new / "proj_000").mkdir(parents=True)
    (new / "FINGERPRINT.json").write_text(json.dumps({"digest": new.name[5:], "components": SOLVE}), encoding="utf-8")
    old = _legacy(repo, {**SOLVE, BKEY: "old-builder"})
    assert me.migrate(repo / "reports" / "mc50", 1, None, rebuild_after=False) == 0
    assert old.is_dir()                                                   # never touched when not needed


def test_an_interrupted_migration_is_completed(repo):
    new = _new_dir(repo)
    (new / "proj_000").mkdir(parents=True)
    (new / "FINGERPRINT.json").write_text(json.dumps({"digest": new.name[5:], "components": SOLVE}), encoding="utf-8")
    (new / "MIGRATED.json").write_text(json.dumps({"from": OLD}), encoding="utf-8")
    m = _marker(repo, 0, OLD)                                             # renamed, but killed before restamping
    assert me.migrate(repo / "reports" / "mc50", 1, None, rebuild_after=False) == 0
    assert m.read_text(encoding="utf-8") == me.digest_of(SOLVE)


def test_builder_stamp_formula_is_shared():
    assert me.builder_digest() == hashlib.sha256(me.BUILDER.read_bytes()).hexdigest()[:12]
