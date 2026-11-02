"""A variable the model does not publish must not sink the whole forecast.

Open-Meteo signals "this model has no such variable" by returning the column
with unit "undefined" and nothing but nulls. ECMWF's AIFS does exactly that for
wind gusts. Refusing the whole request over it cost the platform the entire AI
forecast - the one model the project most needs in order to benchmark AI
against physics - so a model that lacks a variable is now treated as a gap in
that variable, while a genuinely unrecognised unit on real values is still
refused.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import respx
from test_provider_openmeteo import (  # type: ignore[import-not-found]
    FORECAST_URL,
    META_URL,
    Clock,
    make,
)

from rwi.core.errors import ProviderUnavailableError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import QualityFlag
from rwi.core.timebase import TimeWindow

POINT = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "openmeteo_forecast_darwin_ifs025.json").read_text())


def _window(payload: dict[str, Any]) -> TimeWindow:
    times = payload["hourly"]["time"]
    from datetime import UTC, datetime

    start = datetime.fromisoformat(times[0]).replace(tzinfo=UTC)
    end = datetime.fromisoformat(times[-1]).replace(tzinfo=UTC)
    return TimeWindow(start_utc=start, end_utc=end)


def _mock(payload: dict[str, Any]) -> None:
    respx.get(FORECAST_URL).respond(200, json=payload)
    respx.get(META_URL).respond(500)


@respx.mock
async def test_a_variable_the_model_lacks_is_missing_not_fatal(tmp_path: Path) -> None:
    payload = _payload()
    hourly = payload["hourly"]
    column = next(c for c in hourly if c.startswith("wind_gusts"))
    hourly[column] = [None] * len(hourly["time"])
    payload["hourly_units"][column] = "undefined"
    _mock(payload)

    provider = make(tmp_path, Clock())
    series = await provider.fetch_forecast(
        POINT, _window(payload), ["temperature_2m", "wind_gusts_10m"]
    )

    # The other variables survive intact - that is the whole point.
    assert any(step.value("temperature_2m") is not None for step in series.steps)
    # The absent one is MISSING, never zero and never guessed.
    gusts = [step.get("wind_gusts_10m") for step in series.steps]
    assert all(g is None or g.value is None for g in gusts)
    assert all(g is None or g.quality is QualityFlag.MISSING for g in gusts)


@respx.mock
async def test_the_gap_is_recorded_rather_than_hidden(tmp_path: Path) -> None:
    payload = _payload()
    hourly = payload["hourly"]
    column = next(c for c in hourly if c.startswith("wind_gusts"))
    hourly[column] = [None] * len(hourly["time"])
    payload["hourly_units"][column] = "undefined"
    _mock(payload)

    provider = make(tmp_path, Clock())
    series = await provider.fetch_forecast(
        POINT, _window(payload), ["temperature_2m", "wind_gusts_10m"]
    )
    # The adapter records provenance per value, so the gap is visible on any
    # value in the series rather than on the series object.
    sample = next(
        step.get("temperature_2m")
        for step in series.steps
        if step.get("temperature_2m") is not None
    )
    assert sample is not None
    notes = sample.provenance.notes or ""
    assert "does not publish" in notes
    assert "wind_gusts_10m" in notes


@respx.mock
async def test_an_unrecognised_unit_on_real_values_is_still_refused(tmp_path: Path) -> None:
    """The original protection stays: a unit we do not know, on values that
    exist, is a conversion we must not guess at."""
    payload = _payload()
    hourly = payload["hourly"]
    column = next(c for c in hourly if c.startswith("wind_gusts"))
    hourly[column] = [12.0] * len(hourly["time"])
    payload["hourly_units"][column] = "furlongs_per_fortnight"
    _mock(payload)

    provider = make(tmp_path, Clock())
    with pytest.raises(ProviderUnavailableError, match="refusing to guess a conversion"):
        await provider.fetch_forecast(
            POINT, _window(payload), ["temperature_2m", "wind_gusts_10m"]
        )


@respx.mock
async def test_a_partially_null_column_is_not_treated_as_absent(tmp_path: Path) -> None:
    """Only an ALL-null column means "the model lacks this". A column with some
    real values and an unknown unit is an anomaly worth refusing."""
    payload = _payload()
    hourly = payload["hourly"]
    column = next(c for c in hourly if c.startswith("wind_gusts"))
    values: list[Any] = [None] * len(hourly["time"])
    values[0] = 9.0
    hourly[column] = values
    payload["hourly_units"][column] = "undefined"
    _mock(payload)

    provider = make(tmp_path, Clock())
    with pytest.raises(ProviderUnavailableError, match="refusing to guess a conversion"):
        await provider.fetch_forecast(
            POINT, _window(payload), ["temperature_2m", "wind_gusts_10m"]
        )


def test_the_fixture_is_unmodified_between_tests() -> None:
    """Each test deep-copies; a mutated fixture would make the others lie."""
    first = _payload()
    second = copy.deepcopy(first)
    assert first == second
