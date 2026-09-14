"""#160 — step-v FR nuclear/reservoir availability mapping (pure; the lake loader itself is DB-backed)."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from dispatch_model.rolling.fr_availability import nuclear_unavail_daily, reservoir_budget_mwh


def _fa(frac_value=0.8, year=2030, week_budget=None):
    idx = pd.MultiIndex.from_tuples([(0, year, d) for d in range(1, 366)], names=["draw", "year", "doy"])
    frac = pd.Series(frac_value, index=idx)
    return {"nuc_frac": frac, "nuc_draws": 1, "reservoir": (week_budget or {})}


def test_nuclear_unavail_maps_fraction_to_outage_on_reference_calendar():
    fa = _fa(frac_value=0.8)
    nu = nuclear_unavail_daily(fa, target_year=2030, ref_year=2019, draw=0, nuc_cap=50000.0)
    # keyed by REFERENCE-year (2019) dates, full year
    assert dt.date(2019, 1, 1) in nu and dt.date(2019, 12, 31) in nu
    assert len(nu) == 365
    # outage = (1 − 0.8) × 50 000 = 10 000 MW at every day
    assert all(abs(v - 10000.0) < 1e-6 for v in nu.values())


def test_nuclear_unavail_draw_wrap_and_absent_cases():
    fa = _fa()
    # draw 3 wraps mod nuc_draws (=1) → draw 0 → same result, not None
    assert nuclear_unavail_daily(fa, 2030, 2019, 3, 50000.0) is not None
    # a year the lake does not cover → None (projection keeps its proxy)
    assert nuclear_unavail_daily(fa, 2099, 2019, 0, 50000.0) is None
    # non-positive capacity → None
    assert nuclear_unavail_daily(fa, 2030, 2019, 0, 0.0) is None


def test_reservoir_budget_lookup_by_iso_week():
    wk = pd.Timestamp("2030-01-28", tz="UTC")
    w = int(wk.isocalendar().week)
    fa = _fa(week_budget={(2030, w): 4_000_000.0})
    assert reservoir_budget_mwh(fa, 2030, wk) == 4_000_000.0
    # a week with no budget entry → None (window keeps its reference-year reservoir energy)
    assert reservoir_budget_mwh(fa, 2030, pd.Timestamp("2030-06-01", tz="UTC")) is None
    # no reservoir data at all → None
    assert reservoir_budget_mwh(_fa(), 2030, wk) is None


# --- DISPATCH_FR_NUC_AVAIL_SCALE (sensitivity lever) ---------------------------------------------------

def test_nuc_avail_scale_is_relative_and_shape_following(monkeypatch):
    monkeypatch.setenv("DISPATCH_FR_NUC_AVAIL_SCALE", "0.95")
    # 0.8 × 0.95 = 0.76 available → outage (1 − 0.76) × 50 000 = 12 000, i.e. +2 000 MW on the reference
    nu = nuclear_unavail_daily(_fa(frac_value=0.8), 2030, 2019, 0, 50000.0)
    assert all(abs(v - 12000.0) < 1e-6 for v in nu.values())
    # RELATIVE, not one point: a fleet already half out loses half as much absolute MW as a full one
    half = nuclear_unavail_daily(_fa(frac_value=0.4), 2030, 2019, 0, 50000.0)
    assert all(abs(v - 31000.0) < 1e-6 for v in half.values())


def test_nuc_avail_scale_absent_or_one_changes_nothing(monkeypatch):
    base = nuclear_unavail_daily(_fa(frac_value=0.8), 2030, 2019, 0, 50000.0)
    monkeypatch.setenv("DISPATCH_FR_NUC_AVAIL_SCALE", "1")
    assert nuclear_unavail_daily(_fa(frac_value=0.8), 2030, 2019, 0, 50000.0) == base
    monkeypatch.delenv("DISPATCH_FR_NUC_AVAIL_SCALE")
    assert nuclear_unavail_daily(_fa(frac_value=0.8), 2030, 2019, 0, 50000.0) == base


def test_nuc_avail_scale_cannot_push_availability_out_of_range(monkeypatch):
    monkeypatch.setenv("DISPATCH_FR_NUC_AVAIL_SCALE", "1.5")     # 0.8 × 1.5 = 1.2, clamped to 1.0
    nu = nuclear_unavail_daily(_fa(frac_value=0.8), 2030, 2019, 0, 50000.0)
    assert all(abs(v) < 1e-9 for v in nu.values())               # no NEGATIVE outage


# --- the three defects of 2026-09-09, each with the test that would have caught it ---------------------

def _fa_labelled(label, frac_value=0.8, year=2030):
    """A partition carrying ONE draw whose label is `label` — the shape every `availability_mc/wdraw=d`
    actually has, and the shape the old `draw % nuc_draws` lookup could not address unless label == 0."""
    idx = pd.MultiIndex.from_tuples([(label, year, d) for d in range(1, 366)],
                                    names=["draw", "year", "doy"])
    return {"nuc_frac": pd.Series(frac_value, index=idx), "nuc_draws": 1,
            "source": f"availability_mc/wdraw={label}", "reservoir": {}}


def test_a_partition_labelled_other_than_zero_resolves():
    """D1. `availability_mc/wdraw=13` holds one series labelled 13, and the runner passes --draw 13.
    The old lookup asked for `13 % 1 == 0`, missed, returned None, and `fr_window` silently substituted
    the 2019 proxy — which is how 49 of 50 Monte-Carlo draws were priced on one identical nuclear shape."""
    nu = nuclear_unavail_daily(_fa_labelled(13), 2030, 2019, 13, 50000.0)
    assert nu is not None, "a partition labelled 13 must be readable by the caller that asked for draw 13"
    assert all(abs(v - 10000.0) < 1e-6 for v in nu.values())          # (1 − 0.8) × 50 000


def test_label_resolution_is_unchanged_where_labels_are_contiguous():
    """D1. The canonical `availability` layer holds labels 0 and 1, where a count and an identifier
    coincide, so the repair must reproduce the old modulo there EXACTLY — including the wrap."""
    idx = pd.MultiIndex.from_tuples([(d, 2030, doy) for d in (0, 1) for doy in range(1, 366)],
                                    names=["draw", "year", "doy"])
    frac = pd.Series(0.0, index=idx)
    frac.loc[(0, 2030)] = 0.9
    frac.loc[(1, 2030)] = 0.5
    fa = {"nuc_frac": frac, "nuc_draws": 2, "source": "availability", "reservoir": {}}
    def at(d):
        return next(iter(nuclear_unavail_daily(fa, 2030, 2019, d, 10000.0).values()))
    assert abs(at(0) - 1000.0) < 1e-6                                  # label 0 → frac 0.9
    assert abs(at(1) - 5000.0) < 1e-6                                  # label 1 → frac 0.5
    assert abs(at(2) - 1000.0) < 1e-6                                  # wraps to label 0, as before


def test_an_unresolvable_draw_is_announced_not_swallowed(capsys):
    """D1. Returning None is legitimate — the window has a proxy — but it must never be SILENT again."""
    assert nuclear_unavail_daily(_fa_labelled(13), 2099, 2019, 13, 50000.0) is None
    out = capsys.readouterr().out
    assert "NOT APPLIED" in out and "availability_mc/wdraw=13" in out and "proxy" in out


def test_the_denominator_is_installed_not_the_annual_max(tmp_path, monkeypatch):
    """D2. The fraction must be available ÷ INSTALLED. Dividing by the annual max of `available_mw` — the
    numerator's own best day, which is never a full-availability day — put the fraction ~6.8 points high,
    about 4.3 GW of French nuclear never removed from the stack."""
    from dispatch_model.rolling import fr_availability as fa
    days = pd.date_range("2030-01-01", periods=5, freq="D", tz="UTC")
    bt = pd.DataFrame({"draw": 0, "day": days, "technology": "nuclear",
                       "available_mw": [900.0, 800.0, 850.0, 700.0, 900.0],
                       "installed_mw": 1000.0})
    # `lake` is imported INSIDE load_fr_availability, so the patch must land on the source module
    from powersim_core import lake
    monkeypatch.setattr(lake, "read_table",
                        lambda layer, table, **kw: bt if table == "availability_by_tech"
                        else (_ for _ in ()).throw(FileNotFoundError()))
    got = fa.load_fr_availability(None)["nuc_frac"].to_numpy()
    # against installed 1000: 0.90/0.80/0.85/0.70/0.90 — NOT against the max of 900, which would give
    # 1.00/0.889/0.944/0.778/1.00 and would claim two days of perfect availability that never happened
    assert got == pytest.approx([0.90, 0.80, 0.85, 0.70, 0.90])
    assert got.max() < 1.0


def test_a_partition_without_installed_mw_is_refused(monkeypatch):
    """D2. An old partition must not silently revive the defective denominator: 'old results wearing a new
    label' is the failure this repo has already paid for twice."""
    from dispatch_model.rolling import fr_availability as fa
    days = pd.date_range("2030-01-01", periods=3, freq="D", tz="UTC")
    bt = pd.DataFrame({"draw": 0, "day": days, "technology": "nuclear",
                       "available_mw": [900.0, 800.0, 850.0]})          # pre-repair schema
    from powersim_core import lake
    monkeypatch.setattr(lake, "read_table", lambda layer, table, **kw: bt)
    with pytest.raises(ValueError, match="installed_mw"):
        fa.load_fr_availability(None)


def test_a_partially_written_installed_column_is_refused(monkeypatch):
    """The `.any()` version of the guard accepted this, and the NaNs then met `.where(inst > 0, 1.0)` —
    NaN > 0 is False, so each became a denominator of 1.0 and the day reported 100 % availability, ZERO
    outage. Silent, and in the most price-distorting direction there is."""
    from dispatch_model.rolling import fr_availability as fa
    days = pd.date_range("2030-01-01", periods=4, freq="D", tz="UTC")
    bt = pd.DataFrame({"draw": 0, "day": days, "technology": "nuclear",
                       "available_mw": [900.0, 800.0, 850.0, 700.0],
                       "installed_mw": [1000.0, None, None, None]})
    from powersim_core import lake
    monkeypatch.setattr(lake, "read_table", lambda layer, table, **kw: bt)
    with pytest.raises(ValueError, match="installed_mw"):
        fa.load_fr_availability(None)


def test_label_resolution_does_not_depend_on_index_order():
    """`nuclear_unavail_daily` is public and takes a plain dict, so it must not inherit whatever order the
    caller's index happens to carry. With labels appearing as [1, 0], insertion order would send draw 2 to
    label 1; sorted order sends it to label 0, which is what the canonical layer has always done."""
    idx = pd.MultiIndex.from_tuples([(d, 2030, doy) for d in (1, 0) for doy in range(1, 366)],
                                    names=["draw", "year", "doy"])
    frac = pd.Series(0.0, index=idx)
    frac.loc[(0, 2030)] = 0.9
    frac.loc[(1, 2030)] = 0.5
    fa = {"nuc_frac": frac, "nuc_draws": 2, "source": "hand-built", "reservoir": {}}
    def at(d):
        return next(iter(nuclear_unavail_daily(fa, 2030, 2019, d, 10000.0).values()))
    assert abs(at(2) - 1000.0) < 1e-6          # wraps to the SORTED first label (0 → frac 0.9)
    assert abs(at(3) - 5000.0) < 1e-6          # → label 1
