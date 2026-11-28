"""MERRA-2 (pvlib get_merra2 output) to an hourly ANALYSIS GHI series (ADR 0017)."""

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, QualityFlag
from rwi.providers.merra2 import hourly_ghi_series

ASKED = GeoPoint(latitude_deg=-23.798, longitude_deg=133.888)
META = {"latitude": -24.0, "longitude": 133.75}
NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _frame(
    values: list[float], *, start: str = "2024-08-22 02:30", **cols: list[float]
) -> pd.DataFrame:
    index = pd.date_range(start, periods=len(values), freq="1h", tz="UTC")
    return pd.DataFrame({"ghi": values, "ghi_clear": [1000.0] * len(values), **cols}, index=index)


def test_midpoint_stamp_becomes_end_of_interval_label() -> None:
    series = hourly_ghi_series(
        _frame([800.0, 900.0]), META, requested_point=ASKED, retrieved_at_utc=NOW
    )
    assert series.times == [
        datetime(2024, 8, 22, 3, tzinfo=UTC),
        datetime(2024, 8, 22, 4, tzinfo=UTC),
    ]
    assert [s.value("ghi") for s in series.steps] == [800.0, 900.0]
    ghi = series.steps[0].values["ghi"]
    assert ghi.provenance.kind is DataKind.ANALYSIS
    assert ghi.provenance.valid_time_utc == series.times[0]
    assert ghi.provenance.point == GeoPoint(latitude_deg=-24.0, longitude_deg=133.75)
    assert ghi.provenance.requested_point == ASKED
    assert series.kind is DataKind.ANALYSIS and series.model_name == "merra2"


def test_model_clear_sky_flux_is_not_carried() -> None:
    series = hourly_ghi_series(_frame([800.0]), META, requested_point=ASKED, retrieved_at_utc=NOW)
    assert series.variables == {"ghi"}


def test_unmapped_column_name_is_accepted() -> None:
    frame = _frame([700.0]).rename(columns={"ghi": "SWGDN"})
    series = hourly_ghi_series(frame, META, requested_point=ASKED, retrieved_at_utc=NOW)
    assert series.steps[0].value("ghi") == 700.0


def test_nan_and_out_of_range_are_flagged() -> None:
    series = hourly_ghi_series(
        _frame([np.nan, 5000.0]), META, requested_point=ASKED, retrieved_at_utc=NOW
    )
    assert series.steps[0].values["ghi"].quality is QualityFlag.MISSING
    assert series.steps[1].values["ghi"].quality is QualityFlag.OUT_OF_RANGE
    assert all(s.value("ghi") is None for s in series.steps)


def test_unsorted_input_is_sorted() -> None:
    frame = _frame([1.0, 2.0, 3.0]).iloc[::-1]
    series = hourly_ghi_series(frame, META, requested_point=ASKED, retrieved_at_utc=NOW)
    assert [s.value("ghi") for s in series.steps] == [1.0, 2.0, 3.0]


@pytest.mark.parametrize(
    ("frame", "match"),
    [
        (_frame([1.0]).drop(columns=["ghi"]), "no all-sky shortwave"),
        (_frame([1.0], start="2024-08-22 03:00"), "HH:30"),
        (_frame([1.0]).tz_localize(None), "tz-aware"),
    ],
)
def test_rejects_unusable_frames(frame: pd.DataFrame, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        hourly_ghi_series(frame, META, requested_point=ASKED, retrieved_at_utc=NOW)
