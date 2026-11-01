"""Provenance: no number without a source, and no category confusion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rwi.core.errors import UnknownVariableError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance, QualityFlag, measured

RETRIEVED = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)


def _forecast_provenance(valid: datetime | None = None) -> Provenance:
    return Provenance(
        provider="open-meteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        retrieved_at_utc=RETRIEVED,
        valid_time_utc=valid or RETRIEVED + timedelta(hours=3),
        model_name="ecmwf_ifs025",
        model_init_time_utc=RETRIEVED - timedelta(hours=2),
    )


def test_an_observation_must_know_when_it_was_measured() -> None:
    with pytest.raises(ValueError, match="observation_time_utc"):
        Provenance(
            provider="bom",
            dataset="station",
            kind=DataKind.OBSERVATION,
            retrieved_at_utc=RETRIEVED,
        )


def test_a_forecast_must_know_what_time_it_is_for() -> None:
    with pytest.raises(ValueError, match="valid_time_utc"):
        Provenance(
            provider="open-meteo",
            dataset="forecast",
            kind=DataKind.FORECAST,
            retrieved_at_utc=RETRIEVED,
        )


def test_observations_and_forecasts_are_distinguishable() -> None:
    forecast = _forecast_provenance()
    observation = Provenance(
        provider="bom",
        dataset="station",
        kind=DataKind.OBSERVATION,
        retrieved_at_utc=RETRIEVED,
        observation_time_utc=RETRIEVED - timedelta(minutes=10),
    )
    assert forecast.is_prediction and not forecast.is_measurement
    assert observation.is_measurement and not observation.is_prediction


def test_clear_sky_model_output_is_neither_measured_nor_a_forecast() -> None:
    clear_sky = Provenance(
        provider="rwi",
        dataset="pvlib",
        kind=DataKind.CLEAR_SKY_MODEL,
        retrieved_at_utc=RETRIEVED,
    )
    assert not clear_sky.is_measurement
    assert not clear_sky.is_prediction


def test_lead_time_and_age() -> None:
    forecast = _forecast_provenance()
    assert forecast.lead_time_s == pytest.approx(5 * 3600)
    assert forecast.age_s(now=RETRIEVED + timedelta(minutes=30)) == pytest.approx(1800.0)


def test_naive_times_are_rejected_in_provenance() -> None:
    with pytest.raises(Exception, match=r"naive|validation"):
        Provenance(
            provider="x",
            dataset="y",
            kind=DataKind.DERIVED,
            retrieved_at_utc=datetime(2027, 8, 24, 3, 0),
        )


def test_the_unit_comes_from_the_registry_and_a_wrong_one_is_refused() -> None:
    prov = _forecast_provenance()
    assert measured("temperature_2m", 31.4, prov).unit == "degC"
    assert measured("pressure_msl", 101300.0, prov).unit == "Pa"
    assert measured("wind_speed_10m", 7.5, prov).unit == "m/s"
    with pytest.raises(ValueError, match="must be in 'Pa'"):
        Measured(variable="pressure_msl", value=1013.0, unit="hPa", provenance=prov)


def test_an_unknown_variable_is_an_error_not_a_guess() -> None:
    with pytest.raises(UnknownVariableError):
        measured("sharknado_index", 1.0, _forecast_provenance())


def test_a_missing_value_is_flagged_not_zeroed() -> None:
    item = measured("ghi", None, _forecast_provenance())
    assert item.value is None
    assert item.quality is QualityFlag.MISSING
    assert not item.is_usable


def test_freshness_uses_the_reference_time() -> None:
    prov = Provenance(
        provider="bom",
        dataset="station",
        kind=DataKind.OBSERVATION,
        retrieved_at_utc=RETRIEVED,
        observation_time_utc=RETRIEVED - timedelta(minutes=45),
    )
    item = measured("temperature_2m", 24.0, prov)
    assert item.is_fresh(3600.0, now=RETRIEVED)
    assert not item.is_fresh(1800.0, now=RETRIEVED)


def test_label_is_attributable(darwin: GeoPoint) -> None:
    prov = _forecast_provenance().model_copy(update={"point": darwin})
    assert "open-meteo" in prov.label()
    assert "ecmwf_ifs025" in prov.label()
