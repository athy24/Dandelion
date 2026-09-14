"""Solve one growth-moderation factor lambda that lands French demand on a 2050 target.

`lambda` scales the GROWTH INCREMENT of every driver that is a consequence of economic growth, holding the
2025 starting point fixed:

    v'(y) = v(2025) + lambda * (v(y) - v(2025))

lambda = 1 is the reference scenario, lambda = 0 freezes the driver at 2025. It is applied to gdp_index
(tertiary activity), the steel / chemicals / cement indices (industrial output), datacentre_load (the
digital economy) and electrolysis_capacity (industrial hydrogen) — and NOT to population, heat-pump stock
or the efficiency rate, which are demographic and policy paths rather than consequences of GDP, so easing
them would be a different scenario claim.

That grouping is the substantive judgement here: gdp_index ALONE cannot reach the target. Taken to zero
growth it is worth -68 TWh by 2046 against roughly -155 needed, because it enters the model only as
`w_tertiary * gdp` inside one of four components. The bisection therefore moves all six together, and the
target is read off a TREND through the last decade rather than off 2046 itself, since a single year carries
its draw's weather.

CORRECTING THE RECORD: THIS SCRIPT IS NOT WHERE THE SHIPPED MODERATION CAME FROM. Read alone, the six
drivers below look like the whole of the easing that stands in `scenarios.xlsx`. They are not. The single
lambda solved here came out at 0.0908 (`scratchpad/growth_lambda.txt`), which would have put gdp_index at
104.1 in 2046 and GDP growth at +0.24 %/yr — a stagnation no forecaster would defend, and it was rejected
for exactly that reason. What replaced it was the TWO-STAGE solve in `scratchpad/solve_balanced.py`:
lambda_econ fixed by a +1.1 %/yr GDP rate target and applied to the six drivers here, then lambda_elec
bisected on the ELECTRIFICATION stocks this docstring excludes — heat_pump_stock, ev_fleet_cars,
ev_fleet_lcv, ev_fleet_hgv. Both lambdas are on record in `scratchpad/balanced_lambdas.txt` as
0.456587 and 0.518555.

Measured, 2046, `archive/workbook_snapshots/scenarios_BACKUP_growth.xlsx` (the pre-moderation reference)
against the live workbook — the ratio, then the lambda it implies:

    gdp_index               155.0        -> 124.57        x0.804    lambda 0.4566
    steel_index             115.71       -> 107.02        x0.925    lambda 0.4566
    chemicals_index         123.57       -> 110.53        x0.894    lambda 0.4566
    cement_index            107.86       -> 103.51        x0.960    lambda 0.4566
    datacentre_load          10.76       ->   5.33        x0.495    lambda 0.4566
    electrolysis_capacity     10.00      ->   4.78        x0.478    lambda 0.4566
    heat_pump_stock      29 714 286      -> 17 004 129    x0.572    lambda 0.5186
    ev_fleet_cars        22 000 000      -> 12 130 371    x0.551    lambda 0.5186
    ev_fleet_lcv          4 000 000      ->  2 170 508    x0.543    lambda 0.5186
    ev_fleet_hgv            500 000      ->    264 092    x0.528    lambda 0.5186
    population           67 600 000      -> 67 600 000    x1.000    untouched

Two lambdas, uniform within their group, matching `balanced_lambdas.txt` to four decimals; every 2025
anchor unmoved; gdp_index at +2.158 %/yr in the snapshot and +1.100 %/yr live, which is the GDP rate target
of the two-stage solve and not an outcome of any bisection. The break is datable: the workbook at c70090c
still carries 155.0 / 29.71 M / 22 M, and at aba6a5d (2026-08-20) it carries 124.57 / 17 004 129 /
12 130 371, unchanged since. So roughly half of France's demand moderation sits in electrification stocks
that this script never touches, and a reader who takes the six drivers below for the whole story will
misattribute it.

WHAT IT WRITES. `--out`, the moderated workbook: a byte copy of `--in` with the six driver series rewritten
at the solved lambda, everything else identical. Beside it, `<out stem>_lambda.txt` with the lambda to six
decimals, and, while the bisection runs, `.<out stem>.candidate.xlsx` — each trial workbook, rebuilt from
`--in` every iteration so the factor is never applied twice, and removed once the solve lands.

WHY IT NO LONGER WRITES THE SHARED WORKBOOK. It used to evaluate a trial lambda by restoring its own August
snapshot over `../scenarios.xlsx`, editing that file in place and re-importing the demand model, which reads
it — so every trial silently redefined the assumptions of all four models, and by September the restore
alone would have reverted some 77 `dispatch_tyndp` rows and seven commodity prices that had been hand-edited
since. `POWERSIM_SCENARIOS` (powersim_core/scenario.py::resolve) makes that unnecessary: it redirects all
four models at a named workbook for one process only, so the trial workbook can live next to `--out` and the
shared file is opened read-only, once, to be copied. `--in` is never written, moved onto, or restored.

    python -X utf8 -W ignore scripts/solve_demand_growth.py --out ../archive/scenarios_moderated.xlsx
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[2]                 # …/PriceModeling
sys.path.insert(0, str(ROOT / "demand_model"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
pd.set_option("display.width", 240)

#: the 2050 level the moderation is solved against, TWh
TARGET_2050 = 590.0
#: bisection steps on lambda in [0, 1]; 9 halvings resolve it to ~0.002
ITERATIONS = 9

#: the GDP-consequent drivers. See the module docstring for what is deliberately absent — and for what the
#: shipped workbook moderates anyway, through a different script.
DRIVERS = [("demand_macro", "gdp_index"), ("demand_macro", "steel_index"),
           ("demand_macro", "chemicals_index"), ("demand_macro", "cement_index"),
           ("demand_new_large_loads", "datacentre_load"),
           ("demand_new_large_loads", "electrolysis_capacity")]


def read_drivers(inp: Path) -> dict[tuple[str, str], dict[int, float]]:
    """The reference driver series from `inp`, {(sheet, variable): {year: value}}.

    Read ONCE, from the untouched input, and every trial is rebuilt from these numbers — never from the
    previous trial, which would compound the factor.
    """
    orig: dict[tuple[str, str], dict[int, float]] = {}
    for sheet, var in DRIVERS:
        df = pd.read_excel(inp, sheet_name=sheet)
        d = df[(df.variable == var) & (df.scenario == "reference")].sort_values("year")
        orig[(sheet, var)] = dict(zip(d.year.astype(int), d.value.astype(float)))
    return orig


def write_moderated(inp: Path, dest: Path, orig: dict, lam: float) -> None:
    """Copy `inp` to `dest` and rewrite the driver series there at `lam`. `inp` is only ever read."""
    shutil.copyfile(inp, dest)
    wb = load_workbook(dest)
    for sheet, var in DRIVERS:
        ws = wb[sheet]
        hdr = {c.value: i + 1 for i, c in enumerate(ws[1])}
        series = orig[(sheet, var)]
        v0 = series[min(series)]
        for r in range(2, ws.max_row + 1):
            if ws.cell(r, hdr["variable"]).value == var and \
               str(ws.cell(r, hdr["scenario"]).value) == "reference":
                y = int(ws.cell(r, hdr["year"]).value)
                ws.cell(r, hdr["value"]).value = float(v0 + lam * (series[y] - v0))
    wb.save(dest)


def run(workbook: Path) -> pd.Series:
    """Annual French demand (TWh) from step iii, reading `workbook` and nothing else.

    `POWERSIM_SCENARIOS` is set before the import, and `scenario.resolve` reads it at call time, so the
    demand model loads the trial file while its own config still points at the shared workbook. The
    `sys.modules` purge is what makes repeated calls honest: the engine caches its sheets on the instance,
    but module-level state from a previous lambda would otherwise survive into the next one.
    """
    os.environ["POWERSIM_SCENARIOS"] = str(workbook)
    for m in [k for k in list(sys.modules) if k.startswith("demand_model")]:
        del sys.modules[m]
    from demand_model.config import load_config as dm_load
    from demand_model.projection.engine import project_trajectory
    cfg = dm_load(str(ROOT / "demand_model" / "config.yaml"))
    s = project_trajectory(cfg, "reference", realization=0, with_residual=False)
    a = s.resample("YE").sum() / 1e6
    a.index = a.index.year
    return a


def extrap_2050(a: pd.Series) -> float:
    """The trajectory's own 2050 value, from the slope of its last decade (weather makes single years noisy)."""
    tail = a.loc[2037:2046]
    sl, ic = np.polyfit(tail.index.values.astype(float), tail.values, 1)
    return float(sl * 2050 + ic)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="inp", default=str(ROOT / "scenarios.xlsx"),
                    help="reference workbook, READ ONLY")
    ap.add_argument("--out", default=str(ROOT / "archive" / "scenarios_moderated.xlsx"),
                    help="the moderated workbook this writes (a NEW file)")
    ap.add_argument("--target", type=float, default=TARGET_2050, help="2050 demand target, TWh")
    ap.add_argument("--iterations", type=int, default=ITERATIONS, help="bisection steps on lambda")
    a = ap.parse_args()

    inp, out = Path(a.inp).resolve(), Path(a.out).resolve()
    # The whole point of the rewrite: there is no path by which the solve can land on its own input, and
    # none by which it can land on the shared workbook whatever --in happens to be.
    if out == inp:
        raise SystemExit(f"--out is --in ({out}); this script never writes its input")
    if out == (ROOT / "scenarios.xlsx").resolve():
        raise SystemExit(f"--out is the shared workbook ({out}); four models read it, write elsewhere")
    if not inp.exists():
        raise SystemExit(f"--in does not exist: {inp}")
    out.parent.mkdir(parents=True, exist_ok=True)

    #: rebuilt from --in at every trial, beside --out, and removed once the solve lands
    trial = out.parent / f".{out.stem}.candidate.xlsx"
    lam_path = out.parent / f"{out.stem}_lambda.txt"
    print(f"reading  {inp}")
    print(f"writing  {out}")
    print(f"trials   {trial}")

    orig = read_drivers(inp)

    # The baseline is a byte copy of --in evaluated through the same env-var path as every trial, so the
    # reference and the moderated runs differ in the driver cells and in nothing else.
    shutil.copyfile(inp, trial)
    base = run(trial)
    print(f"reference: 2046 {base.loc[2046]:.1f} TWh, extrapolated 2050 {extrap_2050(base):.1f} TWh")

    lo, hi = 0.0, 1.0
    for _i in range(a.iterations):
        mid = (lo + hi) / 2
        write_moderated(inp, trial, orig, mid)
        traj = run(trial)
        e50 = extrap_2050(traj)
        print(f"  lambda={mid:.4f}  2046 {traj.loc[2046]:6.1f}  2050(extrap) {e50:6.1f}", flush=True)
        if e50 > a.target:
            hi = mid
        else:
            lo = mid
    lam = (lo + hi) / 2

    write_moderated(inp, out, orig, lam)
    final = run(out)
    trial.unlink(missing_ok=True)

    print(f"\nSOLVED lambda = {lam:.4f}")
    print(f"  2046 {final.loc[2046]:.1f} TWh   2050 (extrapolated) {extrap_2050(final):.1f} TWh")
    g = pd.read_excel(out, sheet_name="demand_macro")
    g = g[(g.variable == "gdp_index") & (g.scenario == "reference")].set_index("year")["value"]
    g0 = pd.Series(orig[("demand_macro", "gdp_index")]).sort_index()
    print(f"  gdp_index {g.iloc[0]:.1f} -> {g.iloc[-1]:.1f}  "
          f"= {(g.iloc[-1]/g.iloc[0])**(1/(g.index[-1]-g.index[0]))-1:+.2%}/yr "
          f"(was {(g0.iloc[-1]/g0.iloc[0])**(1/(g0.index[-1]-g0.index[0]))-1:+.2%}/yr)")
    print("\n=== trajectory ===")
    print(pd.DataFrame({"reference": base, "moderated": final,
                        "delta": (final - base)}).round(1).to_string())
    lam_path.write_text(f"{lam:.6f}\n", encoding="utf-8")
    print(f"\nwrote {out}\nwrote {lam_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
