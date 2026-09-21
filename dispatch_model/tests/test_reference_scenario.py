"""The REFERENCE scenario is year y of Monte-Carlo draw d(y), in every artefact, or it is refused.

Tests for the tooling of 2026-09-17 (scripts/reference_scenario.py and the scripts it names): the year map selection,
the availability stitch and its label, the French frame stitch, the seam taper, the driver-snapshot sandbox, the
stamps that make every artefact name the map it was built from, and the idempotence that keeps a restarted driver
from invalidating finished sensitivity variants. Everything runs on synthetic data under tmp_path and a tmp lake.
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

import build_reference_availability as bra  # noqa: E402
import build_reference_weather as brw  # noqa: E402
import reference_scenario as refs  # noqa: E402
import run_reference_20y as rr  # noqa: E402
import select_reference_weather as srw  # noqa: E402

FP = "FFFFFFFFFFFF"


# ---- availability ------------------------------------------------------------------------------------------------
def _write_draw_partitions(d: int, years=(2027, 2028, 2029), states=("available", "planned")):
    from powersim_core import lake
    days = pd.date_range(f"{years[0]}-01-01", f"{years[-1]}-12-31", freq="D", tz="UTC")
    bt = pd.concat([pd.DataFrame({"draw": np.int64(d), "day": days, "technology": t,
                                  "available_mw": d * 1000.0 + days.year - 2000 + days.dayofyear / 1000,
                                  "installed_mw": 60000.0}) for t in ("nuclear", "gas")], ignore_index=True)
    nu = pd.DataFrame({"draw": np.int64(d), "day": days, "unit_id": "U1", "available_mw": float(d),
                       "state": pd.Categorical([states[i % len(states)] for i in range(len(days))])})
    ic = pd.DataFrame({"border": "FR-ES", "direction": "export", "day": days, "available_ntc_mw": d * 10.0,
                       "draw": np.int64(d)})
    weeks = pd.date_range(f"{years[0]}-01-04", f"{years[-1]}-12-31", freq="W-MON", tz="UTC")
    rb = pd.DataFrame({"week_start": weeks, "avail_energy_gwh": d * 1000.0 + np.arange(len(weeks))})
    for t, df in (("availability_by_tech", bt), ("availability_nuclear_units", nu), ("interconnectors", ic),
                  ("reservoir_budget", rb)):
        p = lake.table_path("availability_mc", t, wdraw=d)
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p, index=False)


def _complete(mc: Path, d: int, fp: str = FP, later_s: int = 10):
    m = mc / f"draw_{d:03d}" / ".draw_complete"
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(fp, encoding="utf-8")
    t = m.stat().st_mtime_ns + later_s * 1_000_000_000
    os.utime(m, ns=(t, t))


@pytest.fixture
def lake(tmp_path, monkeypatch):
    monkeypatch.setenv("POWERSIM_LAKE", str(tmp_path / "lake"))
    return tmp_path / "lake"


def test_stitch_takes_each_year_from_its_own_draw_under_one_label(tmp_path, lake):
    mc = tmp_path / "mc50"
    _write_draw_partitions(3, states=("available", "planned"))
    _write_draw_partitions(7, states=("available", "forced"))
    _complete(mc, 3)
    _complete(mc, 7)
    meta_path = tmp_path / "map.meta.json"
    meta_path.write_text("{}", encoding="utf-8")
    sel, meta = {2027: 3, 2028: 7, 2029: 3}, {"model_fingerprint": FP, "draws": [3, 7]}

    assert bra.build(sel, meta, wdraw=901, meta_path=meta_path, mc=mc) is True
    from powersim_core import lake as lk
    bt = pd.read_parquet(lk.table_path("availability_mc", "availability_by_tech", wdraw=901))
    assert set(bt["draw"]) == {901}                          # ONE label, or the dispatch falls back to 2019
    by_year = bt[bt["technology"] == "nuclear"].groupby(bt["day"].dt.year)["available_mw"].min()
    assert by_year[2027] // 1000 == 3 and by_year[2028] // 1000 == 7 and by_year[2029] // 1000 == 3
    rb = pd.read_parquet(lk.table_path("availability_mc", "reservoir_budget", wdraw=901))
    assert (rb[rb["week_start"].dt.year == 2028]["avail_energy_gwh"] // 1000 == 7).all()
    nu = pd.read_parquet(lk.table_path("availability_mc", "availability_nuclear_units", wdraw=901))
    assert set(nu["state"].astype(str)) == {"available", "planned", "forced"}
    assert refs.verify_availability(sel, meta, 901, meta_path) == []

    assert bra.build(sel, meta, wdraw=901, meta_path=meta_path, mc=mc) is False      # idempotent

    # a step-v re-projection over the stitch is detected
    p = lk.table_path("availability_mc", "availability_by_tech", wdraw=901)
    bt.assign(draw=np.int64(0)).to_parquet(p, index=False)
    assert any("rewritten" in x for x in refs.verify_availability(sel, meta, 901, meta_path))
    # a map that changed is detected
    assert any("another year map" in x for x in refs.verify_availability({2027: 7, 2028: 7, 2029: 3}, meta, 901,
                                                                         meta_path))


def test_a_source_rewritten_after_its_draw_completed_is_refused(tmp_path, lake):
    mc = tmp_path / "mc50"
    _write_draw_partitions(3)
    _complete(mc, 3, later_s=-100)                           # the marker predates the partition
    with pytest.raises(SystemExit, match="rewritten after draw 3 completed"):
        bra.check_sources({2027: 3}, FP, mc)
    _complete(mc, 3, fp="ANOTHERMODEL")
    with pytest.raises(SystemExit, match="not complete under ensemble"):
        bra.check_sources({2027: 3}, FP, mc)


# ---- the map -----------------------------------------------------------------------------------------------------
def test_constrained_selection_prefers_the_neighbours_among_fr_candidates_and_falls_back_to_fr():
    rows = []
    fr = {2030: [50.0, 50.0, 50.0, 54.0], 2031: [40.0, 60.0, 45.0, 65.0]}
    de = {2030: [70.0, 80.0, 75.0, 75.0], 2031: [70.0, 70.0, 70.0, 70.5]}
    for y in fr:
        for d in range(4):
            rows += [{"year": y, "zone": "FR", "draw": d, "mean_spot": fr[y][d]},
                     {"year": y, "zone": "DE_LU", "draw": d, "mean_spot": de[y][d]}]
    sel, stats = srw.select(pd.DataFrame(rows), fr_tol=1.0)
    assert sel[2030] == 2                                    # FR within 1 of 51: draws 0-2; DE closest: 2
    assert sel[2031] == 1                                    # none within 1 of 52.5: the first FR-closest
    assert stats.loc[2031, "n_candidates"] == 0


def test_load_map_refuses_a_map_that_disagrees_with_its_meta(tmp_path):
    mp, me = tmp_path / "m.json", tmp_path / "m.meta.json"
    mp.write_text(json.dumps({"2027": 3, "2028": 7}), encoding="utf-8")
    me.write_text(json.dumps({"model_fingerprint": FP, "draws": [3, 7], "map": {"2027": 3, "2028": 7}}), "utf-8")
    assert refs.load_map(mp, me)[0] == {2027: 3, 2028: 7}
    me.write_text(json.dumps({"model_fingerprint": FP, "draws": [3, 7], "map": {"2027": 3, "2028": 3}}), "utf-8")
    with pytest.raises(SystemExit, match="does not match"):
        refs.load_map(mp, me)
    mp.write_text(json.dumps({"2027": 3, "2029": 7}), encoding="utf-8")
    me.write_text(json.dumps({"model_fingerprint": FP, "draws": [3, 7], "map": {"2027": 3, "2029": 7}}), "utf-8")
    with pytest.raises(SystemExit, match="not contiguous"):
        refs.load_map(mp, me)


def test_an_identical_selection_rewrites_nothing_and_a_different_one_is_refused(tmp_path, monkeypatch):
    mc = tmp_path / "mc50"
    for d, lvl in enumerate((50.0, 51.0, 52.0)):
        dd = mc / f"draw_{d:03d}"
        dd.mkdir(parents=True)
        (dd / ".draw_complete").write_text(FP, encoding="utf-8")
        pd.DataFrame([{"year": 2030, "zone": z, "mean_spot": lvl + (0 if z == "FR" else d)}
                      for z in ("FR", "DE_LU")]).to_csv(dd / "price_summary.csv", index=False)
    out = tmp_path / "ref" / "map.json"

    def run(*extra):
        monkeypatch.setattr(sys, "argv", ["x", "--mc", str(mc), "--fingerprint", FP, "--out", str(out), *extra])
        return srw.main()

    run()
    meta = out.with_name("map.meta.json")
    before = (out.read_bytes(), meta.read_bytes(), meta.stat().st_mtime_ns)
    run()
    assert (out.read_bytes(), meta.read_bytes(), meta.stat().st_mtime_ns) == before
    with pytest.raises(SystemExit, match="different selection"):
        run("--draws", "0-1")
    run("--draws", "0-1", "--force")
    assert meta.read_bytes() != before[1]


# ---- the cube ----------------------------------------------------------------------------------------------------
def test_verify_cube_names_each_mismatch(tmp_path):
    import xarray as xr
    meta_path = tmp_path / "m.meta.json"
    meta_path.write_text("meta", encoding="utf-8")
    sel = {2027: 3, 2028: 7}
    attrs = {"model_fp": FP, "year_to_draw": json.dumps({"2027": 3, "2028": 7}),
             "map_meta_sha256": refs.file_sha256(meta_path), "year_map_verified": 1}
    cube = tmp_path / "c.nc"
    xr.Dataset({"obs": ("time", np.zeros(3))}, attrs=attrs).to_netcdf(cube)
    assert refs.verify_cube(sel, {"model_fingerprint": FP}, cube, meta_path) == []
    probs = refs.verify_cube({2027: 3, 2028: 3}, {"model_fingerprint": "OTHER"}, cube, meta_path)
    assert any("model_fp" in p for p in probs) and any("year_to_draw" in p for p in probs)
    meta_path.write_text("meta, edited", encoding="utf-8")
    assert any("another version of the map meta" in p for p in refs.verify_cube(sel, {"model_fingerprint": FP},
                                                                                 cube, meta_path))
    assert refs.verify_cube(sel, {"model_fingerprint": FP}, tmp_path / "absent.nc", meta_path)


def test_year_spans_and_the_seam_taper():
    times = pd.date_range("2027-01-01", periods=48, freq="h", tz="UTC").append(
        pd.date_range("2028-01-01", periods=48, freq="h", tz="UTC"))
    spans = brw.year_spans(times, [2027, 2028])
    assert spans == {2027: (0, 48), 2028: (48, 96)}
    buf = np.zeros((96, 2, 1))
    buf[48:] = 10.0
    orig = buf.copy()
    report = brw.taper_seams(buf, spans, seam_hours=12)
    assert report[0][0] == 2028 and report[0][1][0] == pytest.approx(10.0)
    assert abs(buf[48, 0, 0] - buf[47, 0, 0]) < 1e-12                   # the step is gone
    assert np.array_equal(buf[:36], orig[:36]) and np.array_equal(buf[60:], orig[60:])   # only the window moved
    with pytest.raises(SystemExit, match="not contiguous"):
        brw.year_spans(times[:2].append(times[48:50]).append(times[2:4]), [2027])        # 2027, 2028, 2027


# ---- the French frames -------------------------------------------------------------------------------------------
def test_stitch_frames_slices_each_year_from_its_draw():
    idx = pd.date_range("2027-01-01", "2029-12-31 23:00", freq="h", tz="UTC")
    frames = {d: pd.DataFrame({"load_mw": float(d), "res_mw": 1.0, "temp_nat": 2.0}, index=idx) for d in (3, 7)}
    out = rr.stitch_frames({2027: 3, 2028: 7, 2029: 3}, frames)
    assert len(out) == len(idx) and out.index.is_monotonic_increasing
    assert out.groupby(out.index.year)["load_mw"].first().to_dict() == {2027: 3.0, 2028: 7.0, 2029: 3.0}
    with pytest.raises(SystemExit, match="no hours in 2030"):
        rr.stitch_frames({2030: 3}, frames)


def test_the_snapshot_sandbox_forbids_cube_reads_and_writes_and_restores_everything(monkeypatch):
    import powersim_core.weather_cube as wc
    from powersim_core import lake
    monkeypatch.setenv("POWERSIM_WEATHER_CUBE", "C:/some/cube.nc")
    before = (lake.read_table, lake.write_table, wc.cache_is_fresh)
    with rr.drivers_from_snapshot({3}):
        assert os.environ["POWERSIM_WEATHER_CUBE"] == str(rr.NO_CUBE) and not rr.NO_CUBE.exists()
        with pytest.raises(SystemExit, match="must only read"):
            lake.write_table(pd.DataFrame(), "res", "proj_drivers", realization=3)
        assert wc.cache_is_fresh(lake.table_path("res", "proj_drivers", realization=3), "any.nc") is True
        with pytest.raises(SystemExit, match="not a snapshotted driver cache"):
            wc.cache_is_fresh(lake.table_path("res", "proj_drivers", realization=4), "any.nc")
        with pytest.raises(SystemExit, match="not a snapshotted driver cache"):
            wc.cache_is_fresh(lake.table_path("demand", "something_else", realization=3), "any.nc")
    assert (lake.read_table, lake.write_table, wc.cache_is_fresh) == before
    assert os.environ["POWERSIM_WEATHER_CUBE"] == "C:/some/cube.nc"


def test_model_drift_ignores_what_a_variant_may_change(monkeypatch):
    rec = {"res_model/x.py": "a", "workbook:scenarios.xlsx": "w", "env:DISPATCH_X": "1", "param:master_seed": "1"}
    monkeypatch.setattr(rr, "ensemble_components", lambda fp: rec)
    assert rr.model_drift(FP, {**rec, "workbook:s05.xlsx": "v", "env:DISPATCH_Y": "2"}) == []
    assert rr.model_drift(FP, {**rec, "res_model/x.py": "b"}) == ["res_model/x.py"]
    assert rr.model_drift(FP, {k: v for k, v in rec.items() if k != "res_model/x.py"}) == ["res_model/x.py"]


def test_the_demand_measurement_rewrites_nothing_when_unchanged(tmp_path, monkeypatch):
    meta_path = tmp_path / "m.meta.json"
    meta_path.write_text("meta", encoding="utf-8")
    monkeypatch.setattr(refs, "META_PATH", meta_path)
    idx = pd.date_range("2027-01-01", "2028-12-31 23:00", freq="h", tz="UTC")
    stitched = pd.DataFrame({"load_mw": 50_000.0, "res_mw": 20_000.0, "temp_nat": 12.0}, index=idx)
    prov = {"reproduces_ensemble_workbook": True, "workbook_sha256": "w", "fr_sheets_sha256": "s",
            "frame_keys_sha256": "k"}
    sel, meta, out = {2027: 3, 2028: 7}, {"model_fingerprint": FP}, tmp_path / "fr_demand_ref.json"
    rec = rr.measure_demand(sel, meta, stitched, prov, out)
    assert rec["fr_demand_twh"] == {"2027": pytest.approx(438.0), "2028": pytest.approx(439.2)}
    before = (out.read_bytes(), out.stat().st_mtime_ns)
    rr.measure_demand(sel, meta, stitched, prov, out)
    assert (out.read_bytes(), out.stat().st_mtime_ns) == before
    rr.measure_demand(sel, meta, stitched.assign(load_mw=51_000.0), prov, out)
    assert out.read_bytes() != before[0]
    with pytest.raises(SystemExit, match="ensemble's own workbook"):
        rr.measure_demand(sel, meta, stitched, {**prov, "reproduces_ensemble_workbook": False}, out)


# ---- the sensitivity set -----------------------------------------------------------------------------------------
def test_demand_reduction_ramps_linearly_to_its_full_share_of_the_reference_in_2050():
    import make_sensitivities as ms
    ref = {y: 450.0 + 6.0 * (y - 2027) for y in range(2027, 2047)}
    ids = dict(ms.DEMAND_REDUCTIONS)
    assert set(ids) == {"s09_fr_demand_m6", "s10a_fr_demand_m13"}
    assert ids["s09_fr_demand_m6"] == pytest.approx(0.0604, abs=5e-5)
    assert ids["s10a_fr_demand_m13"] == pytest.approx(0.1275, abs=5e-5)
    env, tw, cut = ms.demand_path(0.06, ref)
    assert env.startswith("FR=2027:1.0000|") and env.count("|") == 19 and "2050" not in env    # never extrapolated
    assert tw[2027] == pytest.approx(ref[2027])
    assert cut == pytest.approx(0.06 * 19 / 23)                  # 2046 carries 19/23 of the 2050 reduction
    assert tw[2046] == pytest.approx(ref[2046] * (1 - 0.06 * 19 / 23))
    assert tw[2036] / ref[2036] == pytest.approx(1 - 0.06 * 9 / 23)
    with pytest.raises(ValueError):
        ms.demand_path(0.06, {**ref, 2050: 590.0})


def test_a_variants_fingerprint_moves_with_the_reference_scenario(tmp_path, monkeypatch):
    import model_fingerprint as mf
    import run_sensitivities as runsens
    cube = tmp_path / "ref.nc"
    cube.write_bytes(b"cube")
    meta_path = tmp_path / "m.meta.json"
    meta_path.write_text("meta", encoding="utf-8")
    monkeypatch.setattr(runsens, "CUBE", cube)
    monkeypatch.setattr(refs, "META_PATH", meta_path)
    monkeypatch.setattr(mf, "digest", lambda extra=None, n=12: json.dumps(extra, sort_keys=True))
    avail = {"sha": "A"}
    monkeypatch.setattr(refs, "availability_digest", lambda wdraw=901: avail["sha"])
    variants = [{"id": "s00"}, {"id": "s01", "env": {"DISPATCH_X": "1"}}]
    base = runsens.variant_fingerprints(variants, 2027, 2046, None)
    avail["sha"] = "B"                                       # the stitch was rebuilt
    moved = runsens.variant_fingerprints(variants, 2027, 2046, None)
    assert all(moved[v] != base[v] for v in base)
    meta_path.write_text("meta, another map", encoding="utf-8")
    assert all(runsens.variant_fingerprints(variants, 2027, 2046, None)[v] != moved[v] for v in base)


# ---- review fixes (2026-09-17) -----------------------------------------------------------------------------------
def test_env_drift_refuses_undeclared_switches_and_ignores_managed_ones():
    comp = {"env:DISPATCH_THERMO_EVOL": "1", "res_model/x.py": "a"}
    base = {"DISPATCH_THERMO_EVOL": "1", "POWERSIM_WEATHER_CUBE": "c.nc", "DISPATCH_AVAIL_WDRAW": "901",
            "POWERSIM_RES_OFFSHORE_LOSSES": "1", "POWERSIM_SCENARIOS": "s05.xlsx", "PATH": "x"}
    assert rr.env_drift(comp, set(), base) == []
    assert rr.env_drift(comp, set(), {**base, "DISPATCH_DEMAND_SCALE": "FR=0.9"}) == ["DISPATCH_DEMAND_SCALE"]
    assert rr.env_drift(comp, {"DISPATCH_DEMAND_SCALE"}, {**base, "DISPATCH_DEMAND_SCALE": "FR=0.9"}) == []
    assert rr.env_drift(comp, set(), {k: v for k, v in base.items() if k != "DISPATCH_THERMO_EVOL"}) == [
        "DISPATCH_THERMO_EVOL"]


def test_a_boundary_between_two_years_of_one_draw_is_not_tapered():
    times = pd.date_range("2027-01-01", periods=48, freq="h", tz="UTC").append(
        pd.date_range("2028-01-01", periods=48, freq="h", tz="UTC"))
    spans = brw.year_spans(times, [2027, 2028])
    buf = np.arange(96, dtype=float).reshape(96, 1, 1).repeat(2, axis=1)
    orig = buf.copy()
    assert brw.taper_seams(buf, spans, seam_hours=12, sel={2027: 5, 2028: 5}) == []
    assert np.array_equal(buf, orig)


def test_the_stitch_refuses_to_overwrite_a_draws_partition(tmp_path, lake):
    sel, meta = {2027: 3}, {"model_fingerprint": FP, "draws": list(range(30))}
    for w in (0, 3, 29, 49, 900):
        with pytest.raises(SystemExit, match="would overwrite"):
            bra.build(sel, meta, wdraw=w, meta_path=tmp_path / "m.json", mc=tmp_path)


def test_an_unreadable_provenance_is_a_problem_not_a_crash(tmp_path, lake):
    from powersim_core import lake as lk
    for t in refs.AVAIL_TABLES:
        part = lk.table_path("availability_mc", t, wdraw=901)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"x")
        refs.provenance_path(t, 901).write_text('{"map": {"2027"', encoding="utf-8")       # killed mid-write
    meta_path = tmp_path / "m.meta.json"
    meta_path.write_text("m", encoding="utf-8")
    probs = refs.verify_availability({2027: 3}, {"model_fingerprint": FP}, 901, meta_path)
    assert len(probs) == 4 and all("unreadable" in p for p in probs)


def test_the_previous_cube_is_copied_aside_atomically_and_a_short_copy_is_redone(tmp_path, monkeypatch):
    old = tmp_path / "REFERENCE_WEATHER_20Y.nc"
    old.write_bytes(b"0123456789")
    monkeypatch.setattr(refs, "cube_attrs", lambda path=None: {"model_fp": "OLDFP"})
    keep = brw.keep_aside(old, FP)
    assert keep.name == "REFERENCE_WEATHER_20Y_OLDFP.nc" and keep.read_bytes() == old.read_bytes()
    keep.write_bytes(b"0123")                                # truncated by a kill
    assert brw.keep_aside(old, FP).read_bytes() == old.read_bytes()
    assert brw.keep_aside(old, "OLDFP") is None              # same ensemble: nothing to keep


def test_the_baseline_check_is_due_again_when_its_report_is_missing_or_older(tmp_path):
    import run_sensitivities as runsens
    d = tmp_path / "s00_baseline"
    d.mkdir()
    assert not runsens.baseline_check_due(d, FP)
    (d / ".sens_complete").write_text(FP, encoding="utf-8")
    assert runsens.baseline_check_due(d, FP)
    import reference_gate as gate
    (d / "reference_check.json").write_text(json.dumps({"checker_sha256": refs.file_sha256(gate.CHECKER)}),
                                            encoding="utf-8")
    m = (d / ".sens_complete").stat().st_mtime_ns
    os.utime(d / "reference_check.json", ns=(m + 10**9, m + 10**9))
    assert not runsens.baseline_check_due(d, FP)
    assert not runsens.baseline_check_due(d, "ANOTHER")
    (d / "reference_check.json").write_text("{}", encoding="utf-8")        # written by an older checker
    os.utime(d / "reference_check.json", ns=(m + 10**9, m + 10**9))
    assert runsens.baseline_check_due(d, FP)

