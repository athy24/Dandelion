"""Projected offshore CF applies the losses its level scale was fitted with when `POWERSIM_RES_OFFSHORE_LOSSES` is set.

`cf_scale` is fitted on `offshore_farm_cf` with its default availability and wake loss (product 0.846); applied to the
loss-free curve it clips the fleet at 1.0 below rated wind, which the level anchor turns into a flat ceiling
(res_model/DECISIONS.md, 2026-09-14). Opt-in, read at every call; the default stays the legacy form.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from res_model.calibration.model import CalibratedRes
from res_model.conversion.wind_offshore import offshore_farm_cf

SCALE = 1.1215                                      # the fitted offshore cf_scale of models/calibrated_res.json


def _cal() -> CalibratedRes:
    return CalibratedRes(pv_bias={}, onshore={"specific_power": 250.0, "smoothing_ms": 2.0, "cf_scale": 0.8},
                         offshore={"specific_power": 350.0, "smoothing_ms": 1.5, "cf_scale": SCALE}, hydro={})


def _wind() -> pd.Series:
    idx = pd.date_range("2030-01-01", periods=301, freq="h", tz="UTC")
    return pd.Series(np.linspace(0.0, 30.0, 301), index=idx)             # 0.1 m/s steps across the whole curve


def test_by_default_the_loss_free_curve_is_clipped_below_rated_wind(monkeypatch):
    monkeypatch.delenv("POWERSIM_RES_OFFSHORE_LOSSES", raising=False)
    w = _wind()
    cf = _cal().apply_offshore(w)
    raw = offshore_farm_cf(w, availability=1.0, wake_loss=0.0)
    assert np.allclose(cf, (raw * SCALE).clip(0.0, 1.0))
    at_ceiling = int((cf >= 1.0 - 1e-12).sum())
    on_plateau = int((raw >= raw.max() - 1e-12).sum())
    assert at_ceiling > on_plateau                                          # saturates before the curve's own plateau


def test_the_flag_applies_the_fitted_losses_and_is_read_at_every_call(monkeypatch):
    cal, w = _cal(), _wind()
    monkeypatch.setenv("POWERSIM_RES_OFFSHORE_LOSSES", "1")
    cf = cal.apply_offshore(w)
    assert np.allclose(cf, offshore_farm_cf(w) * SCALE)                    # the form calibration/historical.py fits on
    assert cf.max() < 1.0                                                   # 0.846 x 1.1215 = 0.949: nothing clipped
    monkeypatch.setenv("POWERSIM_RES_OFFSHORE_LOSSES", "0")                # same object, flag flipped: legacy again
    assert np.allclose(cal.apply_offshore(w), (offshore_farm_cf(w, availability=1.0, wake_loss=0.0) * SCALE).clip(0, 1))
