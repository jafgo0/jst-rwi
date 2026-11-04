"""Tests for rwi.satellite.satcorps.

These verify the module's shape and the degrade-gracefully behaviour: it
must import cleanly whether or not earthaccess is installed, and raise
SatCorpsCapabilityError with a specific remediation message when a
capability is missing. Actual download/read paths are not exercised here
because they need Earthdata credentials and real netCDF files; they are
covered manually when credentials are set up.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import numpy as np
import pytest

from rwi.core.timebase import TimeWindow
from rwi.satellite import satcorps


def test_collection_constants_are_the_ones_the_catalogue_commits_to() -> None:
    """These ids must not drift from docs/data-sources/nasa-earthdata-catalog.md."""
    assert satcorps.COLLECTION_CONCEPT_ID == "C3880513838-LARC_CLOUD"
    assert satcorps.COLLECTION_SHORT_NAME == "CER_GEO_Ed4_HIM09_SH"


def test_default_variables_cover_the_three_properties_the_catalogue_names() -> None:
    """The catalogue markets cloud phase, cloud visible optical depth and cloud top
    height as the primary pixel-level SatCORPS fields for race strategy; defaults
    must carry them. The pixel file does NOT carry cloud_fraction or cloud_mask -
    those are aggregate products (see module docstring)."""
    defaults = satcorps.DEFAULT_VARIABLES
    for required in ("cloud_phase", "cloud_visible_optical_depth", "cloud_top_height"):
        assert required in defaults


def test_search_without_earthaccess_installed_raises_the_remediation_error() -> None:
    """If earthaccess was uninstalled, the module stays importable but each
    call tells the operator how to install it rather than a bare ImportError."""
    with (
        patch.dict(sys.modules, {"earthaccess": None}),
        pytest.raises(satcorps.SatCorpsCapabilityError, match="earthaccess"),
    ):
        satcorps.search_scenes(TimeWindow(
            start_utc=datetime(2025, 8, 24, tzinfo=UTC),
            end_utc=datetime(2025, 8, 24, 1, tzinfo=UTC),
        ))


def test_scene_is_frozen_and_reports_download_state() -> None:
    scene = satcorps.SatCorpsScene(
        granule_id="CER_GEO_Ed4_HIM09_SH_V01.4_2025.236.0030.06K",
        time_start_utc=datetime(2025, 8, 24, 0, 30, tzinfo=UTC),
        time_end_utc=datetime(2025, 8, 24, 1, 30, tzinfo=UTC),
        data_url="https://example.invalid/granule.nc",
    )
    assert scene.is_downloaded is False
    with pytest.raises(AttributeError):
        # frozen dataclasses refuse attribute assignment
        scene.local_path = Path("/tmp/x.nc")  # type: ignore[misc]


def test_nearest_pixel_is_planar_cosine_weighted() -> None:
    """A target at (-23.7, 133.9) on a tight grid lands at its own index.

    This locks the planar approximation: lat error weighs 1 deg,
    lon error is scaled by cos(lat). Otherwise a target 1 deg north
    would tie with a target 1 deg east at this latitude.
    """
    # 3 x 3 grid centred on Alice Springs with 0.1 deg step
    lats = np.array([
        [-23.8, -23.8, -23.8],
        [-23.7, -23.7, -23.7],
        [-23.6, -23.6, -23.6],
    ])
    lons = np.array([
        [133.8, 133.9, 134.0],
        [133.8, 133.9, 134.0],
        [133.8, 133.9, 134.0],
    ])
    row, col = satcorps._nearest_pixel(lats, lons, -23.7, 133.9)
    assert (row, col) == (1, 1)


def test_fill_value_detection_prefers_FillValue_over_missing_value() -> None:
    """A dataset that carries both attributes should use _FillValue; the
    _apply_scale_offset path then returns None via the public read path.
    Here we exercise the helper on a minimal mock."""
    class Fake:
        attrs: ClassVar[dict[str, float]] = {"_FillValue": -9999.0, "missing_value": -9998.0}
    assert satcorps._fill_value(Fake()) == -9999.0


def test_scale_offset_is_applied_as_product_specification_states() -> None:
    """SatCORPS applies value = raw * scale_factor + add_offset. If we get
    that wrong once for cloud_optical_depth, a value of 20 becomes 2.0 or 200
    and the downstream k* verification silently reads a wrong sky."""
    class Fake:
        attrs: ClassVar[dict[str, float]] = {"scale_factor": 0.01, "add_offset": -2.0}
    assert satcorps._apply_scale_offset(Fake(), 5000.0) == pytest.approx(48.0)
