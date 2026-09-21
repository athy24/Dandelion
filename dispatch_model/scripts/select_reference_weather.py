"""Select the year -> draw map for REFERENCE_WEATHER_20Y from the completed Monte-Carlo draws of the model on disk.

The reference is ONE weather trajectory whose every year prices at the ensemble's centre, so that a sensitivity run on
it reads as "the ensemble average, with one assumption changed". Each year is taken from one draw, chosen here.

THE CRITERION (chosen 2026-09-17, on draws 0-29 of fingerprint 3724001c75e0). For each projected year:
  1. keep the draws whose FR annual mean spot lies within `--fr-tol` EUR/MWh (default 1.0) of the ensemble mean;
  2. among them, take the one whose OTHER zones sit closest to their own ensemble means: the smallest mean squared
     z-score of the annual mean spot across the non-FR zones (z against the between-draw standard deviation);
  3. if no draw is within the tolerance, the FR-closest draw.
Selecting on FR alone put FR within 0.16 EUR/MWh a year on average but left the neighbours where a random draw would
(mean |z| 0.65); weighting every zone equally brought the neighbours to 0.36 but moved FR by 2.9 EUR/MWh a year. The
constrained rule measured FR |dev| 0.46 mean / 0.91 max EUR/MWh, horizon +0.13, neighbours mean |z| 0.48, 15 draws.
These are ARCHIVE figures -- how close each chosen draw was in its own run. Whether a re-run on the stitched weather
reproduces them is a separate measurement (scripts/check_reference_weather.py).

OUTPUT. `--out` (default archive/reference_20y/ref_weather_map.json) holds the plain {year: draw} map every reference
builder reads (scripts/reference_scenario.py); `<out stem>.meta.json` beside it records the model fingerprint, the
draws the ensemble mean was taken over, the criterion and every year's ensemble statistics and deviations, so the map
can be traced to the ensemble it was chosen on. A map is a DECISION the whole reference scenario and the sensitivity
set are built on: an existing map that differs is never replaced without `--force` (re-running with more completed
draws would otherwise silently redefine the baseline). A different selection belongs in another `--out`.

    python -X utf8 -W ignore scripts/select_reference_weather.py [--draws 0-29] [--fr-tol 1.0] [--out PATH] [--force]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = _HERE.parent
MASTER_SEED = 20260629


def _parse_draws(s: str | None) -> set[int] | None:
    if not s:
        return None
    out: set[int] = set()
    for part in s.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b) + 1) if b else [int(a)])
    return out


def load_ensemble(mc: Path, fp: str, draws: set[int] | None) -> pd.DataFrame:
    """price_summary.csv of every draw completed by model `fp` (optionally restricted to `draws`), long."""
    rows, other = [], 0
    for d in sorted(mc.glob("draw_*")):
        n = int(d.name.split("_")[1])
        if draws is not None and n not in draws:
            continue
        m = d / ".draw_complete"
        if not m.exists():
            continue
        if m.read_text(encoding="utf-8").strip() != fp:
            other += 1
            continue
        rows.append(pd.read_csv(d / "price_summary.csv").assign(draw=n))
    if other:
        print(f"  {other} completed draw(s) belong to another model and are left out")
    if not rows:
        raise SystemExit(f"no draw completed by model {fp} under {mc}")
    return pd.concat(rows, ignore_index=True)


def tables(E: pd.DataFrame, ref_zone: str = "FR") -> dict:
    """Per zone: year x draw annual mean spot (P) and its between-draw z-score (Z); FR deviation from the ensemble
    mean; the neighbours' mean squared z (oscore). The ONE place the criterion's statistics are computed."""
    zones = sorted(E["zone"].unique())
    others = [z for z in zones if z != ref_zone]
    P = {z: E[E["zone"] == z].pivot(index="year", columns="draw", values="mean_spot") for z in zones}
    Z = {z: P[z].sub(P[z].mean(axis=1), axis=0).div(P[z].std(axis=1), axis=0) for z in zones}
    fr_dev = P[ref_zone].sub(P[ref_zone].mean(axis=1), axis=0)
    oscore = sum(Z[z] ** 2 for z in others) / len(others)
    return {"P": P, "Z": Z, "fr_dev": fr_dev, "oscore": oscore, "others": others, "ref_zone": ref_zone}


def year_stats(T: dict, y: int, d: int, n_candidates: int) -> dict:
    """The per-year record the meta file keeps for draw d in year y."""
    P, Z, fr_dev, others, rz = T["P"], T["Z"], T["fr_dev"], T["others"], T["ref_zone"]
    return {"year": int(y), "draw": int(d), "n_draws": int(P[rz].loc[y].notna().sum()),
            "n_candidates": int(n_candidates),
            "fr_ensemble_mean": round(float(P[rz].loc[y].mean()), 4),
            "fr_ensemble_sd": round(float(P[rz].loc[y].std()), 4),
            "fr_draw_mean": round(float(P[rz].loc[y, d]), 4),
            "fr_dev": round(float(fr_dev.loc[y, d]), 4),
            "others_mean_abs_z": round(float(np.mean([abs(Z[z].loc[y, d]) for z in others])), 4),
            "others_max_abs_z": round(float(np.max([abs(Z[z].loc[y, d]) for z in others])), 4)}


def select(E: pd.DataFrame, fr_tol: float, ref_zone: str = "FR") -> tuple[dict[int, int], pd.DataFrame]:
    """Year -> draw under the constrained criterion (module docstring), and the per-year statistics."""
    T = tables(E, ref_zone)
    fr_dev, oscore = T["fr_dev"], T["oscore"]
    sel, stats = {}, []
    for y in T["P"][ref_zone].index:
        cand = list(fr_dev.loc[y][fr_dev.loc[y].abs() <= fr_tol].index)
        fallback = not cand
        if fallback:
            cand = [fr_dev.loc[y].abs().idxmin()]
        d = int(oscore.loc[y, cand].idxmin())
        sel[int(y)] = d
        stats.append(year_stats(T, int(y), d, 0 if fallback else len(cand)))
    return sel, pd.DataFrame(stats).set_index("year")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mc", default=str(REPO / "reports" / "mc50"))
    ap.add_argument("--draws", default=None, help="restrict the ensemble, e.g. 0-29 (default: every completed draw)")
    ap.add_argument("--fr-tol", type=float, default=1.0, help="EUR/MWh around the FR ensemble mean")
    ap.add_argument("--out", default=str(REPO.parent / "archive" / "reference_20y" / "ref_weather_map.json"))
    ap.add_argument("--force", action="store_true", help="replace an existing, different map at --out")
    ap.add_argument("--fingerprint", default=None, help="model whose draws are used (default: the model on disk)")
    a = ap.parse_args()

    from model_fingerprint import digest
    fp = a.fingerprint or digest({"master_seed": MASTER_SEED, "n_weeks": None})      # run_montecarlo defaults
    E = load_ensemble(Path(a.mc), fp, _parse_draws(a.draws))
    draws = sorted(int(d) for d in E["draw"].unique())
    sel, stats = select(E, a.fr_tol)

    pd.set_option("display.width", 200)
    print(f"model {fp}: {len(draws)} draws ({draws[0]}..{draws[-1]}), FR tolerance {a.fr_tol} EUR/MWh")
    print(stats.to_string())
    fd = stats["fr_dev"]
    print(f"FR |dev| mean {fd.abs().mean():.3f} max {fd.abs().max():.3f}, horizon {fd.mean():+.3f} EUR/MWh; "
          f"others mean |z| {stats['others_mean_abs_z'].mean():.3f}; {len(set(sel.values()))} distinct draws")

    out = Path(a.out)
    new_map = {str(y): d for y, d in sel.items()}
    meta = {"model_fingerprint": fp, "master_seed": MASTER_SEED, "draws": draws, "fr_tol_eur_mwh": a.fr_tol,
            "criterion": ("per year: draws with |FR mean spot - ensemble mean| <= fr_tol; among them the smallest mean "
                          "squared z-score of the other zones' annual mean spot; FR-closest if none within fr_tol"),
            "selected_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "map": new_map,
            "per_year": {str(y): r for y, r in stats.drop(columns="draw").to_dict(orient="index").items()}}
    meta_path = out.with_name(out.stem + ".meta.json")
    if out.exists() and meta_path.exists():
        # every reference artefact is stamped with the meta file's sha256: an identical selection rewrites NOTHING
        was = json.loads(meta_path.read_text(encoding="utf-8"))
        same = (json.loads(out.read_text(encoding="utf-8")) == new_map
                and json.loads(json.dumps({k: v for k, v in was.items() if k != "selected_utc"}))
                == json.loads(json.dumps({k: v for k, v in meta.items() if k != "selected_utc"})))
        if same:
            print(f"{out} already holds this selection - unchanged")
            return 0
    if out.exists() and not a.force:
        raise SystemExit(f"{out} holds a different selection, which the reference scenario may be built on: not "
                         f"replaced (write this one to another --out, or pass --force)")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(new_map), encoding="utf-8")
    meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"wrote {out} and {meta_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
