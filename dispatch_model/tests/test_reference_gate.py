"""The sensitivity set launches only on a baseline whose re-run lands within 1 EUR/MWh of the ensemble, every year.

Tests for the gate of 2026-09-17 (scripts/reference_gate.py), the map adjustment it triggers
(scripts/adjust_reference_map.py), the runner's repair of missing or short years (scripts/run_sensitivities.py) and
the reference check's hours and out-of-ensemble draws (scripts/check_reference_weather.py). Synthetic data only.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import adjust_reference_map as adj  # noqa: E402
import check_reference_weather as crw  # noqa: E402
import reference_gate as gate  # noqa: E402
import reference_scenario as refs  # noqa: E402
import run_sensitivities as runsens  # noqa: E402

FP = "FFFFFFFFFFFF"
YEARS = [2030, 2031, 2032]


def _check(gaps: dict[int, float], hours: dict[int, tuple[int, int]] | None = None, meta_sha: str = "M") -> dict:
    per_year = {}
    for y, g in gaps.items():
        rh, oh = (hours or {}).get(y, (8736, 8736))
        per_year[str(y)] = {"draw": 3, "ref": 50 + g, "own": 50.2, "ens_mean": 50.0, "ens_sd": 7.0,
                            "ref_minus_own": g - 0.2, "ref_minus_ens": g, "own_minus_ens": 0.2, "z": g / 7,
                            "ref_hours": rh, "own_hours": oh}
    return {"map_meta_sha256": meta_sha, "zone": "FR", "checker_sha256": refs.file_sha256(gate.CHECKER),
            "fr": {"mean_spot": {"per_year": per_year}}}


# ---- the verdict -------------------------------------------------------------------------------------------------
def test_every_year_must_be_within_one_euro_inclusive_and_on_the_same_hours():
    assert gate.evaluate(_check({2030: 1.0, 2031: -1.0, 2032: 0.3}), YEARS)["passed"]
    v = gate.evaluate(_check({2030: 1.0001, 2031: -0.2, 2032: -1.2}), YEARS)
    assert not v["passed"] and set(v["failing_years"]) == {"2030", "2032"} and v["max_abs_gap"] == 1.2
    v = gate.evaluate(_check({2030: 0.1, 2031: 0.1}), YEARS)
    assert not v["passed"] and v["missing_years"] == [2032]
    v = gate.evaluate(_check({2030: 0.1, 2031: 0.1, 2032: 0.1}, {2031: (8568, 8736)}), YEARS)
    assert not v["passed"] and v["hours_mismatch_years"] == {"2031": {"ref_hours": 8568, "own_hours": 8736}}
    v = gate.evaluate(_check({2030: float("nan"), 2031: 0.1, 2032: 0.1}), YEARS)
    assert not v["passed"] and "2030" in v["failing_years"]    # not a number is not within the tolerance
    assert gate.TOLERANCE == 1.0                               # a constant, never an option


def test_a_stamp_counts_only_for_this_baseline_this_map_and_this_check(tmp_path, monkeypatch):
    meta = tmp_path / "m.meta.json"
    meta.write_text("meta", encoding="utf-8")
    monkeypatch.setattr(refs, "META_PATH", meta)
    run = tmp_path / "s00_baseline"
    run.mkdir()
    (run / ".sens_complete").write_text(FP, encoding="utf-8")
    (run / gate.CHECK).write_text(json.dumps(_check({2030: 0.2, 2031: 0.4, 2032: -0.9},
                                                    meta_sha=refs.file_sha256(meta))), encoding="utf-8")
    m = (run / ".sens_complete").stat().st_mtime_ns
    stamp = gate.write(run, YEARS, FP)
    os.utime(run / gate.STAMP, ns=(m + 10**9, m + 10**9))
    assert stamp["passed"] and gate.gate_passed(run, FP)
    assert not gate.gate_passed(run, "ANOTHERFP")
    (run / gate.CHECK).write_text(json.dumps(_check({2030: 0.2, 2031: 0.4, 2032: -1.9},
                                                    meta_sha=refs.file_sha256(meta))), encoding="utf-8")
    assert not gate.gate_passed(run, FP)                     # the check changed after the verdict
    gate.write(run, YEARS, FP)
    os.utime(run / gate.STAMP, ns=(m + 2 * 10**9, m + 2 * 10**9))
    assert gate.load(run, FP) is not None and not gate.gate_passed(run, FP)
    meta.write_text("meta, adjusted", encoding="utf-8")
    assert gate.load(run, FP) is None                        # another map
    (run / gate.CHECK).write_text(json.dumps(_check({2030: 0.0, 2031: 0.0, 2032: 0.0}, meta_sha="OLD")), "utf-8")
    assert gate.write(run, YEARS, FP)["passed"] is False     # a check measured on another map never passes


def test_a_check_of_another_zone_or_by_another_checker_never_passes(tmp_path, monkeypatch):
    meta = tmp_path / "m.meta.json"
    meta.write_text("meta", encoding="utf-8")
    monkeypatch.setattr(refs, "META_PATH", meta)
    checker = tmp_path / "check_reference_weather.py"
    checker.write_text("v1", encoding="utf-8")
    monkeypatch.setattr(gate, "CHECKER", checker)
    run = tmp_path / "s00_baseline"
    run.mkdir()
    (run / ".sens_complete").write_text(FP, encoding="utf-8")
    good = _check({2030: 0.1, 2031: 0.1, 2032: 0.1}, meta_sha=refs.file_sha256(meta))
    (run / gate.CHECK).write_text(json.dumps({**good, "zone": "DE_LU"}), encoding="utf-8")
    st = gate.write(run, YEARS, FP)
    assert not st["passed"] and "DE_LU" in st["refused"]
    (run / gate.CHECK).write_text(json.dumps(good), encoding="utf-8")
    assert gate.write(run, YEARS, FP)["passed"] and gate.gate_passed(run, FP)
    checker.write_text("v2, a fix to the checker", encoding="utf-8")
    assert gate.load(run, FP) is None                        # an older checker's verdict is not current
    assert "another version" in gate.write(run, YEARS, FP)["refused"]


# ---- the adjustment ----------------------------------------------------------------------------------------------
def _pool(y: int, fr: dict[int, float], de: dict[int, float], hours: dict[int, int] | None = None):
    E = pd.DataFrame([{"year": y, "zone": z, "draw": d, "mean_spot": v}
                      for d in fr for z, v in (("FR", fr[d]), ("DE_LU", de[d]))])
    H = pd.DataFrame([{"year": y, "zone": "FR", "draw": d, "hours": (hours or {}).get(d, 8736)} for d in fr])
    return E, H


def test_a_failing_year_takes_a_draw_predicted_to_pass_with_a_margin_first():
    ens = [0, 1, 2, 3]
    fr = {0: 50.0, 1: 50.3, 2: 49.9, 3: 49.8, 40: 50.05}           # ensemble mean 50.0
    de = {0: 70.0, 1: 75.0, 2: 71.0, 3: 80.0, 40: 70.0}             # DE mean 74: draw 2 closest, then 0, then 3
    E, H = _pool(2033, fr, de)
    t = adj.candidate_table(E, H, ens, 2033)
    assert t.loc[1, "dev"] == pytest.approx(0.3) and not t.loc[40, "in_ensemble"]
    # re-run error 0.5: worst cases 0 -> 0.5, 1 -> 0.8, 2 -> 0.6, 3 -> 0.7; those keeping 0.25 in hand are 0, 2, 3,
    # and among them the neighbours decide
    assert adj.choose(t, 0.5, 1.2, tried=set()) == (2, "ensemble")
    # re-run error 0.85: only 0 (0.85) and 2 (0.95) pass, neither with the margin: the most FR headroom wins
    assert adj.choose(t, 0.85, 1.2, tried=set()) == (0, "ensemble")
    # only buildable draws count
    assert adj.choose(t, 0.5, 1.2, tried=set(), buildable=lambda d: d != 2) == (0, "ensemble")


def test_other_draws_need_the_flag_and_the_last_resort_must_improve_on_the_gap():
    ens = [0, 1, 2, 3]
    fr = {0: 50.0, 1: 50.3, 2: 49.9, 3: 49.8, 40: 50.05}
    de = {0: 70.0, 1: 75.0, 2: 71.0, 3: 80.0, 40: 70.0}
    E, H = _pool(2033, fr, de)
    t = adj.candidate_table(E, H, ens, 2033)
    # re-run error 0.95, draw 0 already tried: no draw of 0-3 is predicted to pass
    assert adj.choose(t, 0.95, 1.2, tried={0}, allow_other=True) == (40, "other-draws")
    # without the flag: the draw predicted closest WITH the sign of the error, |dev + e|: 3 -> 0.75
    assert adj.choose(t, 0.95, 1.2, tried={0}) == (3, "fr-closest-signed")
    # ... and only if that improves on the measured gap
    assert adj.choose(t, 0.95, 0.7, tried={0}) is None
    E2, H2 = _pool(2033, fr, de, hours={3: 8568})                  # draw 3 dropped a window in its own run
    t2 = adj.candidate_table(E2, H2, ens, 2033)
    assert adj.choose(t2, 0.95, 1.2, tried={0}) == (2, "fr-closest-signed")
    assert adj.choose(t2, 0.95, 5.0, tried={0, 1, 2, 3}) is None


def _world(tmp_path, monkeypatch, gaps, hours=None, history=None):
    """A map, its meta, an MC output tree and a failed-gate baseline, all under tmp_path."""
    ref_dir, mc = tmp_path / "ref", tmp_path / "mc50"
    ref_dir.mkdir(parents=True)
    monkeypatch.setattr(refs, "REF_DIR", ref_dir)
    monkeypatch.setattr(refs, "MAP_PATH", ref_dir / "ref_weather_map.json")
    monkeypatch.setattr(refs, "META_PATH", ref_dir / "ref_weather_map.meta.json")
    rng = np.random.default_rng(0)
    for d in range(6):
        dd = mc / f"draw_{d:03d}"
        dd.mkdir(parents=True)
        (dd / ".draw_complete").write_text(FP, encoding="utf-8")
        rows, nz = [], []
        for y in YEARS:
            for z, base in (("FR", 50.0), ("DE_LU", 70.0)):
                rows.append({"year": y, "zone": z, "mean_spot": base + (d - 2.5) * 0.4 + rng.normal(0, 0.01)})
                nz.append({"year": y, "zone": z, "hours": 8736})
        pd.DataFrame(rows).to_csv(dd / "price_summary.csv", index=False)
        pd.DataFrame(nz).to_csv(dd / "negative_zero_hours.csv", index=False)
    sel = {2030: 0, 2031: 1, 2032: 2}
    meta = {"model_fingerprint": FP, "draws": [0, 1, 2, 3, 4], "map": {str(y): d for y, d in sel.items()},
            "per_year": {}, **({"adjustments": history} if history else {})}
    refs.MAP_PATH.write_text(json.dumps(meta["map"]), encoding="utf-8")
    refs.META_PATH.write_text(json.dumps(meta), encoding="utf-8")
    run = tmp_path / "s00_baseline"
    run.mkdir()
    (run / gate.CHECK).write_text(json.dumps(_check(gaps, hours, refs.file_sha256(refs.META_PATH))), "utf-8")
    gate.write(run, YEARS, FP)
    return run, mc


def _adjust(monkeypatch, run, mc, *extra):
    monkeypatch.setattr(sys, "argv", ["adjust", "--run", str(run), "--mc", str(mc), *extra])
    monkeypatch.setattr(adj, "build_problems", lambda d, fp, mc: [])   # synthetic draws: every one buildable
    return adj.main()


def test_the_adjustment_rewrites_only_the_failing_years_and_keeps_the_history(tmp_path, monkeypatch):
    run, mc = _world(tmp_path, monkeypatch, {2030: 1.3, 2031: 0.2, 2032: -0.5})
    before_meta = refs.META_PATH.read_bytes()
    assert _adjust(monkeypatch, run, mc) == 0
    sel, meta = refs.load_map()
    assert sel[2031] == 1 and sel[2032] == 2 and sel[2030] == 1    # |dev + e| = |-0.4 + 1.1| = 0.7 < 1.3
    rec = meta["adjustments"]
    assert len(rec) == 1 and rec[0]["round"] == 1 and rec[0]["year"] == 2030 and rec[0]["from_draw"] == 0
    assert rec[0]["tier"] == "fr-closest-signed" and meta["includes_draws_outside_selection"] is False
    assert meta["per_year"]["2030"]["adjusted_round"] == 1 and meta["draws"] == [0, 1, 2, 3, 4]
    assert (refs.REF_DIR / "history" / "round0_ref_weather_map.meta.json").read_bytes() == before_meta
    # the stamp now names the previous map: acting on it again is refused
    assert _adjust(monkeypatch, run, mc) == 2


def test_the_adjustment_refuses_solver_failures_passed_gates_and_exhausted_rounds(tmp_path, monkeypatch):
    run, mc = _world(tmp_path / "a", monkeypatch, {2030: 0.1, 2031: 0.2, 2032: -0.5})
    assert _adjust(monkeypatch, run, mc) == 3
    run, mc = _world(tmp_path / "b", monkeypatch, {2030: 1.5, 2031: 0.2, 2032: -0.5}, hours={2030: (8568, 8736)})
    assert _adjust(monkeypatch, run, mc) == 2 and refs.load_map()[0][2030] == 0   # the re-run itself is short
    hist = [{"round": r, "year": 2031, "from_draw": 5, "to_draw": 1} for r in (1, 2, 3)]
    run, mc = _world(tmp_path / "c", monkeypatch, {2030: 1.5, 2031: 0.2, 2032: -0.5}, history=hist)
    assert _adjust(monkeypatch, run, mc) == 2 and refs.load_map()[0][2030] == 0


def test_a_draw_whose_own_run_was_short_is_replaced(tmp_path, monkeypatch):
    run, mc = _world(tmp_path, monkeypatch, {2030: 0.1, 2031: 0.2, 2032: -0.5}, hours={2030: (8736, 8568)})
    assert _adjust(monkeypatch, run, mc) == 0
    sel, meta = refs.load_map()
    assert sel[2030] == 2 and meta["adjustments"][0]["reason"] == "own run short"


def test_a_failed_write_leaves_the_previous_map_and_meta_in_place(tmp_path, monkeypatch):
    run, mc = _world(tmp_path, monkeypatch, {2030: 1.3, 2031: 0.2, 2032: -0.5})
    before = (refs.MAP_PATH.read_bytes(), refs.META_PATH.read_bytes())
    real = adj._replace

    def flaky(tmp, dest, attempts=6):
        if Path(dest) == refs.MAP_PATH:
            raise PermissionError("held by OneDrive")
        return real(tmp, dest, attempts)

    monkeypatch.setattr(adj, "_replace", flaky)
    assert _adjust(monkeypatch, run, mc) == 1
    assert (refs.MAP_PATH.read_bytes(), refs.META_PATH.read_bytes()) == before
    assert refs.load_map()[0][2030] == 0


# ---- the runner's repair of dropped windows ----------------------------------------------------------------------
def test_short_and_missing_years_are_found_set_aside_and_split_across_workers(tmp_path, monkeypatch):
    mc = tmp_path / "mc50"
    for d, h32 in ((0, 8736), (1, 8736), (2, 8568)):
        dd = mc / f"draw_{d:03d}"
        dd.mkdir(parents=True)
        pd.DataFrame([{"year": y, "zone": "FR", "hours": 8736 if y != 2032 else h32} for y in YEARS]
                     + [{"year": 2032, "zone": "DE_LU", "hours": 1}]).to_csv(dd / "negative_zero_hours.csv",
                                                                             index=False)
    monkeypatch.setattr(refs, "load_map", lambda *a, **k: ({}, {"draws": [0, 1, 2]}))
    expected = runsens.expected_hours(YEARS, mc)
    assert expected == {2030: 8736, 2031: 8736, 2032: 8736}
    run = tmp_path / "s01"
    run.mkdir()
    idx = pd.date_range("2019-01-01", periods=8736, freq="h", tz="UTC")
    for y, n in ((2030, 8736), (2031, 8568)):
        px = pd.concat({"smc": pd.DataFrame({"FR": 1.0}, index=idx[:n]), "spot": pd.DataFrame({"FR": 1.0},
                                                                                            index=idx[:n])}, axis=1)
        px.to_parquet(run / f"prices_{y}.parquet")
        pd.DataFrame({"x": [1]}).to_parquet(run / f"dispatch_{y}.parquet")
    assert runsens.bad_years(run, YEARS, expected) == [2031, 2032]
    assert runsens.bad_years(run, YEARS, None) == [2032]         # smoke runs: only absence counts
    assert runsens.set_aside_short(run, [2031, 2032]) == [2031]
    assert not (run / "prices_2031.parquet").exists() and not (run / "dispatch_2031.parquet").exists()
    assert len(list((run / "_short").glob("2031-*/prices_2031.parquet"))) == 1
    assert runsens.split_years(list(range(2027, 2047)), 3) == [list(range(2027, 2047))[i::3] for i in range(3)]
    assert runsens.split_years([2031], 3) == [[2031]]


def test_every_run_pins_the_hash_seed():
    assert runsens._base_env()["PYTHONHASHSEED"] == "0"


def test_a_rebuilt_workbook_with_the_same_cells_is_recognised(tmp_path):
    import make_sensitivities as ms
    from openpyxl import Workbook
    paths = []
    for i, v in enumerate((1.0, 1.0, 2.0)):
        wb = Workbook()
        wb.active.title = "dispatch_commodities"
        wb.active.append(["commodity", "year", "price"])
        wb.active.append(["co2", 2035, v])
        wb.active["D2"] = "=C2*2"
        p = tmp_path / f"w{i}.xlsx"
        wb.save(p)
        paths.append(p)
    assert ms.same_content(paths[0], paths[1])
    assert not ms.same_content(paths[0], paths[2])


# ---- the reference check -----------------------------------------------------------------------------------------
def test_the_check_reads_a_replacement_draws_own_run_but_keeps_the_ensemble_centre():
    E = pd.DataFrame([{"year": 2033, "zone": "FR", "draw": d, "mean_spot": v} for d, v in ((0, 49.0), (1, 51.0))])
    O = pd.DataFrame([{"year": 2033, "zone": "FR", "draw": 40, "mean_spot": 50.4}])
    R = pd.DataFrame([{"year": 2033, "zone": "FR", "mean_spot": 50.9}])
    c = crw.compare({2033: 40}, E, R, "mean_spot", "FR", O)
    assert c.loc[2033, "own"] == pytest.approx(50.4) and c.loc[2033, "ens_mean"] == pytest.approx(50.0)
    assert c.loc[2033, "ref_minus_own"] == pytest.approx(0.5) and c.loc[2033, "ref_minus_ens"] == pytest.approx(0.9)
