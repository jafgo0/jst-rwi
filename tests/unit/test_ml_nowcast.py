"""rwi.ml.nowcast: the nowcaster seam and patch extraction."""

from datetime import UTC, datetime

import numpy as np
import pytest

from rwi.contracts import GeoGrid, SatelliteScene
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance
from rwi.ml.nowcast import (
    AdvectionNowcaster,
    NeuralNowcaster,
    Nowcaster,
    PersistenceNowcaster,
    extract_patch,
)

T = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)


def _scene(n: int = 10) -> SatelliteScene:
    lats = [-20.0 - 0.01 * i for i in range(n)]
    lons = [134.0 + 0.01 * j for j in range(n)]
    return SatelliteScene(
        satellite="himawari-9", band="C03", slot_time_utc=T,
        grid=GeoGrid(latitudes=lats, longitudes=lons, shape=(n, n), resolution_km=1.0,
                     bounds=(min(lats), min(lons), max(lats), max(lons))),
        quantity="cloud_index", unit="1", solar_zenith_range_deg=(30.0, 40.0),
        provenance=Provenance(provider="test", dataset="synthetic", kind=DataKind.DERIVED,
                              retrieved_at_utc=T),
    )


def test_all_nowcasters_satisfy_the_protocol() -> None:
    for nc in (PersistenceNowcaster(), AdvectionNowcaster(), NeuralNowcaster()):
        assert isinstance(nc, Nowcaster)


def test_neural_nowcaster_refuses_until_trained() -> None:
    scene = _scene()
    with pytest.raises(NotImplementedError):
        NeuralNowcaster().nowcast(
            GeoPoint(latitude_deg=-20.05, longitude_deg=134.05), [scene], [np.zeros((10, 10))],
            horizon_s=3600.0, step_s=600.0, clear_sky=lambda _t: 800.0,
        )


def test_extract_patch_centre_edge_and_shape_mismatch() -> None:
    scene = _scene()
    values = np.arange(100, dtype=float).reshape(10, 10)
    pt = GeoPoint(latitude_deg=-20.05, longitude_deg=134.05)
    patch = extract_patch(scene, values, pt, 2)
    assert patch is not None and patch.shape == (5, 5)
    assert patch[2, 2] == values[5, 5]
    patch[0, 0] = -1.0
    assert values[3, 3] != -1.0  # a copy, not a view
    corner = GeoPoint(latitude_deg=-20.0, longitude_deg=134.0)
    assert extract_patch(scene, values, corner, 1) is None
    off_grid = GeoPoint(latitude_deg=-30.0, longitude_deg=134.0)
    assert extract_patch(scene, values, off_grid, 0) is None
    with pytest.raises(ValueError):
        extract_patch(scene, np.zeros((3, 3)), pt, 1)
    with pytest.raises(ValueError):
        extract_patch(scene, values, pt, -1)
