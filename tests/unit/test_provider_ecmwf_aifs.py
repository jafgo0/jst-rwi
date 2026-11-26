"""ECMWF AIFS GRIB decoding against one real, cropped open-data run (ADR 0008 §1).

The fixture is genuine ECMWF output (provenance in
`tests/fixtures/ecmwf_aifs/README.md`), so these tests pin the decoder to the
real file layout and GRIB units rather than to a hand-built dataset. They need
the optional ``[ml]`` extra and are skipped without it.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from rwi.core.errors import ProviderUnavailableError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind
from rwi.core.timebase import TimeWindow
from rwi.providers import ecmwf_aifs
from rwi.providers.ecmwf_aifs import AIFS_PARAMS, run_from_grib
from rwi.providers.gridded import BBox, series_from_run

pytest.importorskip("cfgrib")

FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "ecmwf_aifs"
    / "aifs_single_20261126T06Z_corridor.grib2"
)
INIT = datetime(2026, 11, 26, 6, tzinfo=UTC)
RETRIEVED = datetime(2026, 11, 26, 13, 52, tzinfo=UTC)
BOX = BBox(south=-20.0, north=-18.0, west=133.0, east=135.0)
POINT = GeoPoint(latitude_deg=-19.6, longitude_deg=134.2)


@pytest.fixture(scope="module")
def run():
    return run_from_grib([FIXTURE], BOX, retrieved_at_utc=RETRIEVED)


def test_run_identity_and_provenance(run):
    assert run.provider == "ecmwf"
    assert run.model_name == "aifs"
    assert run.kind is DataKind.FORECAST
    assert run.license == "CC-BY-4.0"
    assert run.init_time_utc == INIT
    assert run.retrieved_at_utc == RETRIEVED
    assert run.valid_times_utc == tuple(INIT + timedelta(hours=h) for h in (0, 6, 12, 18, 24))
    assert set(run.fields) == set(AIFS_PARAMS.values())


def test_grid_is_cropped_to_bbox_plus_one_node(run):
    assert run.latitudes.min() == pytest.approx(-20.25)
    assert run.latitudes.max() == pytest.approx(-17.75)
    assert run.longitudes.min() == pytest.approx(132.75)
    assert run.longitudes.max() == pytest.approx(135.25)
    for arr in run.fields.values():
        assert arr.shape == (5, run.latitudes.size, run.longitudes.size)


def test_units_are_converted_into_physical_ranges(run):
    t = run.fields["temperature_2m"]
    assert np.all((t > 10.0) & (t < 50.0))  # degC, not K
    for name in ("cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"):
        assert np.all((run.fields[name] >= 0.0) & (run.fields[name] <= 100.0))
    assert np.all(
        (run.fields["pressure_msl"] > 95_000.0) & (run.fields["pressure_msl"] < 105_000.0)
    )
    assert np.all(run.fields["precipitation_accum"] >= 0.0)


def test_ssrd_accumulates_and_is_flat_over_the_night(run):
    # 06-12 h after a 06Z init is 12-18 UTC, i.e. night in central Australia.
    ssrd = run.fields["ssrd_accum"]
    assert np.all(ssrd[0] == 0.0)
    assert np.all(np.diff(ssrd, axis=0) >= 0.0)
    assert np.array_equal(ssrd[2], ssrd[1])


def test_hourly_series_has_zero_irradiance_at_night(run):
    window = TimeWindow(start_utc=INIT + timedelta(hours=1), end_utc=INIT + timedelta(hours=24))
    series = series_from_run(run, POINT, window, ["ghi", "dni", "temperature_2m"])
    assert series.kind is DataKind.FORECAST
    assert series.model_init_time_utc == INIT
    for step in series.steps:
        ghi, dni = step.values["ghi"].value, step.values["dni"].value
        assert ghi is not None and dni is not None
        assert ghi >= 0.0 and dni >= 0.0
    night = [s for s in series.steps if 13 <= s.valid_time_utc.hour <= 18]
    assert len(night) == 6
    assert all(s.values["ghi"].value == 0.0 for s in night)
    assert all(s.values["dni"].value == 0.0 for s in night)
    assert any((s.values["ghi"].value or 0.0) > 100.0 for s in series.steps)


def test_bbox_outside_the_file_is_refused():
    with pytest.raises(ProviderUnavailableError):
        run_from_grib(
            [FIXTURE],
            BBox(south=-35.0, north=-33.0, west=138.0, east=139.0),
            retrieved_at_utc=RETRIEVED,
        )


def test_unknown_units_are_refused_not_guessed(monkeypatch):
    table = {k: v for k, v in ecmwf_aifs._CONVERT.items() if k[0] != "temperature_2m"}
    monkeypatch.setattr(ecmwf_aifs, "_CONVERT", table)
    with pytest.raises(ProviderUnavailableError, match="unexpected units"):
        run_from_grib([FIXTURE], BOX, retrieved_at_utc=RETRIEVED)
