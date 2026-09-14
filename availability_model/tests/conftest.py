"""Session-scoped fixtures: the DB-derived artifacts are built once for the whole test session.

Combined with the on-disk scan cache (io/cache.py), this turns a multi-hour suite into a few minutes.
"""
from __future__ import annotations

import pytest
from availability_model.config import load_config


@pytest.fixture(scope="session", autouse=True)
def _isolate_lake(tmp_path_factory):
    """No test may write the PRODUCTION lake, and one did.

    `test_projection.py` calls `pipeline.project(cfg, n_draws=2)`, which defaults to `layer="availability"`
    and writes it — so running the suite silently replaced the canonical availability tables that the
    dispatch consumes and that `tools/golden.py` fingerprints. The canonical layer had no provenance at
    all: it was whatever a two-draw test fixture last produced, on whatever cube happened to be
    configured. Measured twice on 2026-09-09, mtimes moving at 09:44 and again at 10:12.

    `lake.lake_root()` resolves `POWERSIM_LAKE` per call, so redirecting it for the session contains every
    write this suite makes — not just the one that was noticed.
    """
    import os
    root = tmp_path_factory.mktemp("lake")
    old = os.environ.get("POWERSIM_LAKE")
    os.environ["POWERSIM_LAKE"] = str(root)
    yield root
    os.environ.pop("POWERSIM_LAKE", None)
    if old is not None:
        os.environ["POWERSIM_LAKE"] = old


@pytest.fixture(scope="session")
def cfg():
    c = load_config("config.yaml")
    if not c.resolve(c.section("data")["sqlite_path"]).exists():
        pytest.skip("pricemodeling DB not present")
    return c


@pytest.fixture(scope="session")
def registry(cfg):
    from availability_model.io.fleet import build_fleet_registry
    return build_fleet_registry(cfg)


@pytest.fixture(scope="session")
def events(cfg, registry):
    from availability_model.io.outages import infer_outage_events
    return infer_outage_events(cfg, registry)


@pytest.fixture(scope="session")
def model(cfg):
    from availability_model.pipeline import calibrate
    return calibrate(cfg)
