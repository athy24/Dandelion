"""Prove every sensitivity actually moves the input it claims to move, before a night is spent on it.

The failure this exists to catch is not a wrong number, it is a SILENT one: a cell edited in a tab nothing
reads, so the variant runs the base case and its "sensitivity" is zero. This repo has produced that defect
repeatedly — `dispatch_tyndp`'s `demand_twh` is read for France and never applied; the Carbon Price Support
was wired into a function the backtest does not call; a Monte-Carlo reused twelve-day-old parquets. Every
one looked like a completed run.

So each variant is loaded THROUGH THE LOADER THE PROJECTION USES, with that variant's `POWERSIM_SCENARIOS`
set exactly as the runner sets it, and the quantity it is supposed to move is compared against the
baseline. A variant whose input does not move fails, and the set does not run.

Environment-flag variants have no workbook cell to inspect. The FR demand paths are probed LIVE through the parser
the projection uses (`ENV_PROBES`: no change in the horizon's first year, a cut in its last); the FR nuclear
availability scale lands inside the step-v mapping and is covered by
`test_fr_availability.py::test_nuc_avail_scale_*` instead -- listed as such rather than silently skipped.

    python -u -X utf8 -W ignore scripts/gate_sensitivities.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO.parent
MANIFEST = ROOT / "archive" / "sensitivities" / "manifest.json"

from dispatch_model.config import load_config  # noqa: E402
from dispatch_model.tyndp import load_ntc_newbuild, load_tyndp, ntc_delta_mw, tyndp_factors  # noqa: E402

cfg = load_config(str(REPO / "config.yaml"))
WB = cfg.resolve(cfg.section("assumptions")["workbook"])


def _under(workbook: str | None, fn):
    """Run `fn` with `POWERSIM_SCENARIOS` set as the runner sets it for that variant."""
    old = os.environ.get("POWERSIM_SCENARIOS")
    if workbook:
        os.environ["POWERSIM_SCENARIOS"] = workbook
    else:
        os.environ.pop("POWERSIM_SCENARIOS", None)
    try:
        return fn()
    finally:
        os.environ.pop("POWERSIM_SCENARIOS", None)
        if old is not None:
            os.environ["POWERSIM_SCENARIOS"] = old


# ---- the probes: each returns one number the variant must move -----------------------------------------
def probe_commodity(name: str, year: int = 2035):
    """Annual mean of the MONTHLY series the dispatch consumes, not the workbook cell — the cell is
    interpolated and seasonally shaped before anything prices against it."""
    def f():
        from dispatch_model.commodities.model import CommodityModel
        mp = CommodityModel.from_workbook(WB).monthly_prices(year, year)
        return float(mp.loc[mp["commodity"] == name, "price"].mean())
    return f


def probe_fr_res(techs: tuple[str, ...], year: int = 2035):
    def f():
        from res_model.config import load_config as rm_load
        from res_model.io.assumptions import load_assumptions
        rcfg = rm_load(str(ROOT / "res_model" / "config.yaml"))
        cap = load_assumptions(rcfg.resolve(rcfg.section("assumptions")["workbook"]))[
            "capacity_trajectories"]
        m = cap["technology"].isin(techs) & (cap["year"] == year)
        return float(cap.loc[m, "capacity_mw"].sum())
    return f


def probe_tyndp_cap(zone: str, tech: str, year: int):
    """The CAP FACTOR the projection actually applies (`tyndp_factors(...)['cap'][tech]`), not the cell."""
    def f():
        t = load_tyndp(WB)
        return float((tyndp_factors(t, zone, year, 2019).get("cap") or {}).get(tech) or 0.0)
    return f


def probe_tyndp_res(zone: str, year: int):
    def f():
        t = load_tyndp(WB)
        return float(tyndp_factors(t, zone, year, 2019).get("res") or 0.0)
    return f


def probe_ntc(a: str, b: str, year: int):
    def f():
        return float(ntc_delta_mw(load_ntc_newbuild(WB), (a, b), year, 2019))
    return f


def probe_large_load(variable: str, year: int):
    """A `demand_new_large_loads` value, read through the demand model's own assumptions loader."""
    def f():
        from demand_model.config import load_config as dm_load
        from demand_model.io.assumptions import load_assumptions
        dcfg = dm_load(str(ROOT / "demand_model" / "config.yaml"))
        t = load_assumptions(dcfg.resolve(dcfg.section("assumptions")["workbook"]))["new_large_loads"]
        m = (t["variable"] == variable) & (t["year"] == year)
        return float(t.loc[m, "value"].iloc[0])
    return f


def probe_demand_factor(year: int):
    """The multiplier `projection._demand_scale` returns for FR in `year` under this variant's env.

    An ENV variant gets a live probe like every other, not just a unit test: the unit test proves the
    parser works, this proves THIS VARIANT'S STRING parses to the intended number in the intended year.
    A path with a typo would still parse -- and would silently apply 1.0.
    """
    def f():
        from dispatch_model.rolling.projection import _demand_scale
        return float(_demand_scale("FR", year))
    return f


def _under_env(env: dict, fn):
    """Run `fn` with this variant's env flags set, exactly as the runner sets them."""
    old = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return fn()
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


#: variant id -> (what the probe measures, probe, direction: -1 down / +1 up)
PROBES = {
    "s01_co2_175":            ("CO2 price 2035 (EUR/t)", probe_commodity("co2"), -1),
    "s02_co2_150":            ("CO2 price 2035 (EUR/t)", probe_commodity("co2"), -1),
    "s03_gas_30":             ("gas price 2035 (EUR/MWh)", probe_commodity("gas"), -1),
    "s04_gas_25":             ("gas price 2035 (EUR/MWh)", probe_commodity("gas"), -1),
    "s05_fr_solar_m10":       ("FR PV 2035 (MW)",
                               probe_fr_res(("pv_utility", "pv_distributed", "pv_btm")), -1),
    "s06_fr_solar_m20":       ("FR PV 2035 (MW)",
                               probe_fr_res(("pv_utility", "pv_distributed", "pv_btm")), -1),
    "s07_fr_wind_m10":        ("FR onshore 2035 (MW)", probe_fr_res(("wind_onshore",)), -1),
    "s08_fr_wind_m20":        ("FR onshore 2035 (MW)", probe_fr_res(("wind_onshore",)), -1),
    "s10b_fr_h2_2gw":         ("FR electrolysis GW 2046", probe_large_load("electrolysis_capacity", 2046), -1),
    "s13_fr_retire_60":       ("FR nuclear cap factor 2044", probe_tyndp_cap("FR", "nuclear", 2044), -1),
    "s14_fr_wind_repower_p5": ("FR onshore 2035 (MW)", probe_fr_res(("wind_onshore",)), +1),
    "s15_nonfr_solar_p10":    ("DE_LU res factor 2035", probe_tyndp_res("DE_LU", 2035), +1),
    "s16_nonfr_wind_p10":     ("DE_LU res factor 2035", probe_tyndp_res("DE_LU", 2035), +1),
    "s17_fres_ntc_p2000":     ("FR-ES NTC delta 2035 (MW)", probe_ntc("FR", "ES", 2035), +1),
    "s18_es_nuc_60":          ("ES nuclear cap factor 2044", probe_tyndp_cap("ES", "nuclear", 2044), +1),
}

#: env-flag variants whose flag IS probed live, as (what, probe, direction). The demand paths must be 1.0
#: in the horizon's first year (the cut ramps, it does not step) and below 1.0 at its last.
ENV_PROBES = {
    "s09_fr_demand_m6":     [("FR demand factor 2027", probe_demand_factor(2027), 0),
                             ("FR demand factor 2046", probe_demand_factor(2046), -1)],
    "s10a_fr_demand_m13":   [("FR demand factor 2027", probe_demand_factor(2027), 0),
                             ("FR demand factor 2046", probe_demand_factor(2046), -1)],
}

#: env-flag variants with no readable input of their own -- the derate lands inside the step-v mapping, so
#: the unit test is the coverage.
ENV_COVERED = {
    "s11_fr_nuc_avail_m1": "tests/test_fr_availability.py::test_nuc_avail_scale_is_relative_and_shape_following",
    "s12_fr_nuc_avail_m5": "tests/test_fr_availability.py::test_nuc_avail_scale_is_relative_and_shape_following",
}


def main() -> int:
    variants = {v["id"]: v for v in json.loads(MANIFEST.read_text(encoding="utf-8"))}
    base = variants["s00_baseline"]["workbook"]
    print(f"baseline workbook: {base}\n")
    print(f"{'variant':24s} {'probe':30s} {'baseline':>12s} {'variant':>12s} {'change':>10s}  verdict")
    fails = []
    for vid, (what, probe, direction) in PROBES.items():
        v = variants[vid]
        b = _under(base, probe)
        x = _under(v["workbook"], probe)
        moved = (x - b) * direction > 1e-9
        pct = (x / b - 1) * 100 if b else float("nan")
        print(f"{vid:24s} {what:30s} {b:12.3f} {x:12.3f} {pct:9.2f}%  "
              f"{'ok' if moved else 'FAIL — input did not move'}")
        if not moved:
            fails.append(vid)

    print()
    for vid, probes in ENV_PROBES.items():
        v = variants[vid]
        for what, probe, direction in probes:
            b = _under_env({}, probe)
            x = _under_env(v["env"], probe)
            # direction 0 = must NOT move (the ramp starts at 1.0); otherwise must move that way
            ok = abs(x - b) < 1e-9 if direction == 0 else (x - b) * direction > 1e-9
            print(f"{vid:24s} {what:30s} {b:12.4f} {x:12.4f} {(x/b-1)*100:9.2f}%  "
                  f"{'ok' if ok else 'FAIL'}")
            if not ok:
                fails.append(f"{vid}:{what}")

    print()
    for vid, test in ENV_COVERED.items():
        v = variants[vid]
        if not v.get("env"):
            fails.append(vid)
            print(f"{vid:24s} FAIL — no env flag in the manifest")
        else:
            print(f"{vid:24s} env {v['env']}  covered by {test}")

    if fails:
        print(f"\n{len(fails)} variant(s) do NOT move their input: {fails}")
        return 1
    print(f"\nall {len(PROBES)} workbook variants move their input; "
          f"{len(ENV_PROBES)} demand-path variants probed live; "
          f"{len(ENV_COVERED)} covered by unit tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
