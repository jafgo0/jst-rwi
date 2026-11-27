"""QuantileBand construction, u/v box projection and per-point reasons (ADR 0011)."""

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance, QualityFlag, Uncertainty
from rwi.core.wind import met_to_uv, relative_wind
from rwi.ml.bands import (
    BOX_METHOD,
    band_from_measured,
    box_projection_bands,
    project_uv,
    route_point_bands,
)

POINT = GeoPoint(latitude_deg=-19.65, longitude_deg=134.19)
INIT = datetime(2027, 8, 22, 0, 0, tzinfo=UTC)
CUTOFF = datetime(2027, 8, 1, 0, 0, tzinfo=UTC)
ETA = INIT + timedelta(hours=6)


@dataclass(frozen=True)
class _FakeModel:
    source_vocabulary: tuple[str, ...] = ("open-meteo/best_match",)


def _models(*variables: str) -> dict[str, Any]:
    return {v: (_FakeModel(), None) for v in variables}


def _ml_measured(
    variable: str,
    p10: float,
    p50: float,
    p90: float,
    *,
    version: str = "v1",
    quality: QualityFlag = QualityFlag.OK,
    when: datetime = ETA,
) -> Measured:
    unit = "W/m2" if variable == "ghi" else "m/s"
    prov = Provenance(
        provider="rwi",
        dataset="ml-postprocess",
        kind=DataKind.FORECAST,
        retrieved_at_utc=INIT,
        valid_time_utc=when,
        model_name=f"pp-{variable}",
        model_version=version,
        model_init_time_utc=INIT,
        training_cutoff_utc=CUTOFF,
        point=POINT,
        processing="quantile post-processing of open-meteo/best_match",
    )
    return Measured(
        variable=variable,
        value=p50,
        unit=unit,
        provenance=prov,
        quality=quality,
        uncertainty=Uncertainty(
            lower=p10,
            upper=p90,
            interval_probability=0.8,
            method="quantile_regression:lightgbm",
        ),
    )


def _upstream(values: dict[str, float], *, provider: str = "open-meteo") -> WeatherSeries:
    units = {"ghi": "W/m2", "wind_speed_10m": "m/s", "wind_direction_10m": "deg"}
    steps = []
    for h in range(0, 13):
        t = INIT + timedelta(hours=h)
        prov = Provenance(
            provider=provider,
            dataset="forecast",
            kind=DataKind.FORECAST,
            retrieved_at_utc=INIT,
            valid_time_utc=t,
            model_name="best_match",
            model_init_time_utc=INIT,
            point=POINT,
        )
        steps.append(
            WeatherStep(
                valid_time_utc=t,
                values={
                    k: Measured(variable=k, value=v, unit=units[k], provenance=prov)
                    for k, v in values.items()
                },
            )
        )
    return WeatherSeries(
        point=POINT,
        provider=provider,
        dataset="forecast",
        kind=DataKind.FORECAST,
        model_name="best_match",
        model_init_time_utc=INIT,
        retrieved_at_utc=INIT,
        steps=steps,
    )


def _ml(*measured: Measured) -> WeatherSeries:
    return WeatherSeries(
        point=POINT,
        provider="rwi",
        dataset="ml-postprocess",
        kind=DataKind.FORECAST,
        model_name="pp",
        model_init_time_utc=INIT,
        retrieved_at_utc=INIT,
        steps=[WeatherStep(valid_time_utc=ETA, values={m.variable: m for m in measured})],
    )


@pytest.mark.science
@pytest.mark.parametrize("heading", [0.0, 37.5, 90.0, 180.0, 251.0, 359.0])
def test_projection_matches_relative_wind_at_zero_vehicle_speed(heading: float) -> None:
    # Reference: rwi.core.wind.relative_wind (vehicle frame) with the vehicle stationary,
    # so the apparent wind is the true wind; u/v from met_to_uv (meteorological from-dir).
    for speed, direction in itertools.product((0.0, 3.0, 11.5), (0.0, 45.0, 135.0, 300.0)):
        u, v = met_to_uv(speed, direction)
        head, cross = project_uv(u, v, heading)
        ref = relative_wind(
            vehicle_speed_mps=0.0,
            vehicle_heading_deg=heading,
            wind_speed_mps=speed,
            wind_from_deg=direction,
        )
        assert head == pytest.approx(ref.headwind_mps, abs=1e-9)
        assert cross == pytest.approx(ref.crosswind_mps, abs=1e-9)


@pytest.mark.science
def test_northbound_southerly_wind_is_a_tailwind_and_westerly_is_from_the_left() -> None:
    # Heading 0 (north). Wind from 180 deg blows north: pure tailwind (headwind < 0).
    head, cross = project_uv(*met_to_uv(5.0, 180.0), 0.0)
    assert head == pytest.approx(-5.0) and cross == pytest.approx(0.0, abs=1e-9)
    # Wind from 270 deg (west) hits a north-bound vehicle on its left side.
    head, cross = project_uv(*met_to_uv(5.0, 270.0), 0.0)
    assert head == pytest.approx(0.0, abs=1e-9) and cross == pytest.approx(5.0)


@pytest.mark.parametrize("heading", [0.0, 30.0, 135.0, 222.0, 315.0])
def test_box_contains_every_corner_and_p50_is_the_median_projection(heading: float) -> None:
    u = band_from_measured(_ml_measured("wind_u_10m", -4.0, -1.0, 2.5, version="u7"))
    v = band_from_measured(_ml_measured("wind_v_10m", 1.0, 3.0, 6.0, version="v9"))
    assert u is not None and v is not None
    head, cross = box_projection_bands(u, v, heading)
    corners = [project_uv(cu, cv, heading) for cu in (u.p10, u.p90) for cv in (v.p10, v.p90)]
    for band, idx in ((head, 0), (cross, 1)):
        values = [c[idx] for c in corners]
        assert band.p10 == pytest.approx(min(values))
        assert band.p90 == pytest.approx(max(values))
        assert band.p50 == pytest.approx(project_uv(u.p50, v.p50, heading)[idx])
        assert band.method == BOX_METHOD and band.interval_probability is None
        assert band.ml_derived and band.unit == "m/s"
        assert band.provenance.model_version == "u:u7+v:v9"
        assert band.provenance.training_cutoff_utc == CUTOFF
        assert band.provenance.kind is DataKind.FORECAST


def test_box_quality_is_ok_only_when_both_marginals_are() -> None:
    u = band_from_measured(_ml_measured("wind_u_10m", -1.0, 0.0, 1.0))
    v = band_from_measured(
        _ml_measured("wind_v_10m", -1.0, 0.0, 1.0, quality=QualityFlag.UNVALIDATED)
    )
    assert u is not None and v is not None
    head, cross = box_projection_bands(u, v, 90.0)
    assert head.quality is cross.quality is QualityFlag.UNVALIDATED


def test_band_needs_an_interval_and_ml_bands_need_lineage() -> None:
    m = _ml_measured("ghi", 300.0, 400.0, 500.0)
    band = band_from_measured(m)
    assert band is not None and band.ml_derived and band.interval_probability == 0.8
    assert band_from_measured(m.model_copy(update={"uncertainty": None})) is None
    no_lineage = m.model_copy(
        update={"provenance": m.provenance.model_copy(update={"training_cutoff_utc": None})}
    )
    with pytest.raises(ValidationError, match="training_cutoff_utc"):
        band_from_measured(no_lineage)


def test_point_bands_give_bands_and_reasons_never_raw_upstream() -> None:
    upstream = _upstream({"ghi": 650.0, "wind_speed_10m": 6.0, "wind_direction_10m": 120.0})
    ml = _ml(
        _ml_measured("ghi", 500.0, 600.0, 700.0),
        _ml_measured("wind_u_10m", -6.0, -5.0, -4.0),
        _ml_measured("wind_v_10m", 2.0, 3.0, 4.0),
    )
    result = route_point_bands(
        upstream,
        ml,
        models=_models("ghi", "wind_u_10m", "wind_v_10m"),
        eta_utc=ETA,
        heading_deg=170.0,
        variables=("ghi", "headwind_10m", "crosswind_10m", "temperature_2m"),
    )
    assert set(result.bands) == {"ghi", "headwind_10m", "crosswind_10m"}
    assert result.bands["ghi"].p50 == 600.0  # the ML P50, not the upstream 650
    assert result.reasons == {"temperature_2m": "no_model"}


def test_point_bands_reasons() -> None:
    upstream = _upstream({"ghi": 650.0, "wind_speed_10m": 6.0, "wind_direction_10m": 120.0})
    models = _models("ghi", "wind_u_10m", "wind_v_10m")
    wanted = ("ghi", "headwind_10m")

    none = route_point_bands(
        None, None, models=models, eta_utc=ETA, heading_deg=0.0, variables=wanted
    )
    assert none.reasons == {"ghi": "no_upstream", "headwind_10m": "no_upstream"}

    foreign = route_point_bands(
        _upstream({"ghi": 650.0}, provider="met-norway"),
        None,
        models=models,
        eta_utc=ETA,
        heading_deg=0.0,
        variables=("ghi",),
    )
    assert foreign.reasons == {"ghi": "not_applicable"}

    # u has an ML value, v does not (outside its training envelope at this step).
    partial = route_point_bands(
        upstream,
        _ml(_ml_measured("wind_u_10m", -1.0, 0.0, 1.0)),
        models=models,
        eta_utc=ETA,
        heading_deg=0.0,
        variables=wanted,
    )
    assert partial.bands == {}
    assert partial.reasons == {
        "ghi": "outside_training_range",
        "headwind_10m": "outside_training_range",
    }

    late = route_point_bands(
        upstream,
        None,
        models=models,
        eta_utc=ETA + timedelta(hours=20),
        heading_deg=0.0,
        variables=("ghi",),
    )
    assert late.reasons == {"ghi": "no_upstream"}
