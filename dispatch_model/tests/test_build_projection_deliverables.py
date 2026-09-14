"""The hourly export must be a time series, not twenty years interleaved on one reference calendar.

Every per-year `prices_<y>.parquet` is indexed on the REFERENCE-year calendar, because `projection._align`
lays each target year's weather onto it by position. The builder used to concatenate the years and call
`sort_index()`, which interleaved them hour by hour -- 174,624 rows all stamped 2019 -- while every yearly
aggregate, computed before the concat, stayed correct. These tests pin the export to the target-year clock.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BUILDER = Path(__file__).resolve().parents[1] / "scripts" / "build_projection_deliverables.py"


def _write_year(src: Path, y: int, idx: pd.DatetimeIndex, level: float) -> None:
    spot = pd.DataFrame({"FR": level + np.arange(len(idx)) * 0.01,
                         "DE_LU": level + 5 + np.arange(len(idx)) * 0.01}, index=idx)
    px = pd.concat({"smc": spot - 1.0, "spot": spot}, axis=1)
    px.index.name = "timestamp_utc"
    px.to_parquet(src / f"prices_{y}.parquet")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    src = tmp_path_factory.mktemp("src")
    out = tmp_path_factory.mktemp("out")
    ref = pd.date_range("2019-01-01", periods=96, freq="h", tz="UTC")
    # 2027 lost a 24 h window (as a window dropped on the solve budget does); 2028 is a leap year, complete
    _write_year(src, 2027, ref.delete(range(48, 72)), level=10.0)
    _write_year(src, 2028, ref, level=1000.0)
    r = subprocess.run([sys.executable, "-X", "utf8", "-W", "ignore", str(BUILDER), "--src", str(src),
                        "--out", str(out)], cwd=str(BUILDER.parents[1]), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    h = pd.read_csv(out / "hourly_spot_prices.csv", parse_dates=["timestamp_utc"])
    return src, out, h


def test_hourly_export_is_strictly_increasing(built):
    _, _, h = built
    assert h["timestamp_utc"].is_monotonic_increasing
    assert h["timestamp_utc"].is_unique


def test_each_year_is_stamped_on_its_own_calendar_and_not_interleaved(built):
    _, _, h = built
    y = h["timestamp_utc"].dt.year
    assert sorted(y.unique()) == [2027, 2028]
    assert (h.loc[y == 2027, "FR"] < 100).all()                       # 2027 level 10.x, 2028 level 1000.x
    assert (h.loc[y == 2028, "FR"] >= 1000).all()
    assert y.is_monotonic_increasing                                  # every 2027 hour before every 2028 hour


def test_hours_map_by_offset_from_january_first_and_gaps_stay_gaps(built):
    _, _, h = built
    ts = set(h["timestamp_utc"])
    assert pd.Timestamp("2027-01-01 00:00") in ts and pd.Timestamp("2027-01-02 23:00") in ts
    assert not any(pd.Timestamp("2027-01-03") + pd.Timedelta(hours=k) in ts for k in range(24))
    assert pd.Timestamp("2027-01-04 00:00") in ts
    assert pd.Timestamp("2028-01-01 00:00") in ts and pd.Timestamp("2028-01-04 23:00") in ts
    assert (h["timestamp_utc"].dt.year == 2027).sum() == 72
    assert (h["timestamp_utc"].dt.year == 2028).sum() == 96


def test_values_follow_their_hour(built):
    _, _, h = built
    first = h[h["timestamp_utc"] == pd.Timestamp("2027-01-04 00:00")]["FR"].iloc[0]
    assert first == pytest.approx(10.0 + 48 * 0.01, abs=0.006)        # 49th surviving hour of the 2027 file


def test_yearly_aggregates_unchanged(built):
    src, out, _ = built
    ps = pd.read_csv(out / "price_summary.csv")
    for y in (2027, 2028):
        px = pd.read_parquet(src / f"prices_{y}.parquet")
        got = float(ps[(ps["year"] == y) & (ps["zone"] == "FR")]["mean_spot"].iloc[0])
        assert got == pytest.approx(float(px[("spot", "FR")].mean()), abs=1e-9)


def test_deliverables_carry_the_builder_version(built):
    _, out, _ = built
    stamp = (out / ".deliverables_fingerprint").read_text(encoding="utf-8").strip()
    assert stamp == hashlib.sha256(BUILDER.read_bytes()).hexdigest()[:12]
