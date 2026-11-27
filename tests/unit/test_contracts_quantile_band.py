"""QuantileBand contract and the derived u/v variables (ADR 0011)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from rwi.contracts import QuantileBand
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance
from rwi.core.variables import CANONICAL_VARIABLES, DERIVED_VARIABLES, spec_for

T = datetime(2027, 8, 22, 3, 0, tzinfo=UTC)
P = GeoPoint(latitude_deg=-16.25, longitude_deg=133.37)


def _prov(**kw: object) -> Provenance:
    base: dict[str, object] = {
        "provider": "rwi",
        "dataset": "ml-postprocess",
        "kind": DataKind.FORECAST,
        "retrieved_at_utc": T - timedelta(hours=1),
        "valid_time_utc": T,
        "model_name": "ghi-qr",
        "model_version": "1",
        "training_cutoff_utc": T - timedelta(days=3),
    }
    base.update(kw)
    return Provenance(**base)  # type: ignore[arg-type]


def _band(**kw: object) -> QuantileBand:
    base: dict[str, object] = {
        "variable": "ghi",
        "unit": "W/m2",
        "valid_time_utc": T,
        "point": P,
        "p10": 400.0,
        "p50": 600.0,
        "p90": 750.0,
        "method": "quantile_regression:lightgbm",
        "interval_probability": 0.8,
        "ml_derived": True,
        "provenance": _prov(),
    }
    base.update(kw)
    return QuantileBand(**base)  # type: ignore[arg-type]


def test_valid_band() -> None:
    band = _band()
    assert band.p10 <= band.p50 <= band.p90
    assert band.provenance.model_version == "1"


def test_quantiles_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="p10 <= p50 <= p90"):
        _band(p50=800.0)


def test_unit_comes_from_the_registry() -> None:
    with pytest.raises(ValidationError, match="W/m2"):
        _band(unit="kW/m2")


def test_route_relative_wind_has_its_own_unit_table() -> None:
    band = _band(variable="headwind_10m", unit="m/s", p10=-3.0, p50=1.0, p90=4.0,
                 method="uv_box_projection", interval_probability=None)
    assert band.interval_probability is None
    with pytest.raises(ValidationError):
        _band(variable="headwind_10m", unit="km/h", p10=-3.0, p50=1.0, p90=4.0)


def test_band_is_never_an_observation() -> None:
    prov = _prov(kind=DataKind.OBSERVATION, observation_time_utc=T)
    with pytest.raises(ValidationError, match="OBSERVATION"):
        _band(provenance=prov)


@pytest.mark.parametrize("missing", ["model_version", "training_cutoff_utc"])
def test_ml_band_needs_model_version_and_cutoff(missing: str) -> None:
    with pytest.raises(ValidationError, match="model_version"):
        _band(provenance=_prov(**{missing: None}))


def test_non_ml_band_may_omit_model_lineage() -> None:
    prov = _prov(provider="open-meteo", dataset="ensemble", model_version=None,
                 training_cutoff_utc=None)
    band = _band(ml_derived=False, provenance=prov, method="ensemble_percentiles")
    assert band.ml_derived is False


def test_valid_time_must_match_provenance_and_is_utc() -> None:
    with pytest.raises(ValidationError, match="valid_time_utc"):
        _band(valid_time_utc=T + timedelta(hours=1))
    acst = timezone(timedelta(hours=9, minutes=30))
    band = _band(valid_time_utc=T.astimezone(acst))
    assert band.valid_time_utc.utcoffset() == timedelta(0)


def test_uv_are_derived_not_canonical() -> None:
    # Providers must cover CANONICAL_VARIABLES exactly; none serves u/v.
    for name in ("wind_u_10m", "wind_v_10m"):
        assert name in DERIVED_VARIABLES
        assert name not in CANONICAL_VARIABLES
        spec = spec_for(name)
        assert (spec.unit, spec.minimum, spec.maximum) == ("m/s", -120.0, 120.0)
