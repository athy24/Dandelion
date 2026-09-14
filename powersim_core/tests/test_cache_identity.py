"""A weather-driver cache is fresh only for the cube it was built from (see weather_cube.cache_is_fresh)."""
from __future__ import annotations

import os

from powersim_core.weather_cube import cache_is_fresh, record_cache_source


def test_a_cache_is_fresh_only_for_the_cube_it_was_built_from(tmp_path):
    a = tmp_path / "a.nc"
    a.write_bytes(b"cube-a")
    b = tmp_path / "b.nc"
    b.write_bytes(b"cube-b")
    cache = tmp_path / "lake" / "realization=0" / "part.parquet"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"partition")
    assert not cache_is_fresh(cache, a)                       # no sidecar: a partition from before is rebuilt
    record_cache_source(cache, a)
    assert cache_is_fresh(cache, a)
    assert not cache_is_fresh(cache, b)                       # same realization number, another cube
    st = a.stat()
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert not cache_is_fresh(cache, a)                       # the same path, regenerated
