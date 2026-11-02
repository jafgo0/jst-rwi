"""Bias correction: only where evidence is meaningful, never physically impossible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rwi.contracts import ForecastObservationPair, VerificationScore, WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, measured
from rwi.core.variables import CANONICAL_VARIABLES
from rwi.verification import apply_bias_correction, bias_correction, score_pairs

T0 = datetime(2027, 10, 20, 0, 0, tzinfo=UTC)
NOW = datetime(2027, 10, 25, 0, 0, tzinfo=UTC)
POINT = GeoPoint(latitude_deg=-12.4, longitude_deg=130.9)
H = 3600.0
PROVIDER = "open-meteo"
MODEL = "ecmwf_ifs025"


def make_pair(
    variable: str, forecast: float, observed: float, *, lead_h: float, k: int
) -> ForecastObservationPair:
    valid = T0 + timedelta(hours=k)
    return ForecastObservationPair(
        variable=variable,
        valid_time_utc=valid,
        forecast_value=forecast,
        observed_value=observed,
        lead_time_s=lead_h * H,
        provider=PROVIDER,
        model_name=MODEL,
        forecast_point=POINT,
        observation_point=POINT,
        separation_m=0.0,
        time_offset_s=0.0,
        forecast_recorded_at_utc=valid - timedelta(seconds=lead_h * H),
    )


def scores_for(
    variable: str, bias: float, *, n: int, lead_h: float = 8.0, base: float = 20.0
) -> list[VerificationScore]:
    unit = CANONICAL_VARIABLES[variable].unit
    pairs = [make_pair(variable, base + bias, base, lead_h=lead_h, k=k) for k in range(n)]
    return score_pairs(pairs, variable=variable, unit=unit, now=NOW)


def forecast_series(
    values: dict[str, list[float]], *, init: datetime | None = T0, first_lead_h: float = 8.0
) -> WeatherSeries:
    n = len(next(iter(values.values())))
    steps = []
    for i in range(n):
        valid = T0 + timedelta(hours=first_lead_h + i)
        prov = Provenance(
            provider=PROVIDER,
            dataset="forecast",
            kind=DataKind.FORECAST,
            retrieved_at_utc=T0,
            valid_time_utc=valid,
            model_name=MODEL,
            model_init_time_utc=init,
            processing="nearest_grid_point",
        )
        steps.append(
            WeatherStep(
                valid_time_utc=valid,
                values={name: measured(name, vals[i], prov) for name, vals in values.items()},
            )
        )
    return WeatherSeries(
        point=POINT,
        provider=PROVIDER,
        dataset="forecast",
        kind=DataKind.FORECAST,
        model_name=MODEL,
        model_init_time_utc=init,
        retrieved_at_utc=T0,
        steps=steps,
    )


# --------------------------------------------------------------------------- bias_correction


def test_bias_correction_sign_is_negative_of_bias() -> None:
    scores = scores_for("temperature_2m", 1.5, n=50)
    assert scores[0].bias == 1.5
    assert (
        bias_correction(
            scores,
            variable="temperature_2m",
            provider=PROVIDER,
            model_name=MODEL,
            lead_time_s=8 * H,
        )
        == -1.5
    )
    cold = scores_for("temperature_2m", -0.8, n=50)
    assert bias_correction(
        cold,
        variable="temperature_2m",
        provider=PROVIDER,
        model_name=MODEL,
        lead_time_s=8 * H,
    ) == pytest.approx(0.8)


def test_thin_evidence_gives_no_correction_fifty_gives_one() -> None:
    kwargs = {
        "variable": "temperature_2m",
        "provider": PROVIDER,
        "model_name": MODEL,
        "lead_time_s": 8 * H,
    }
    thin = scores_for("temperature_2m", 1.5, n=5)
    assert thin[0].is_meaningful is False
    assert bias_correction(thin, **kwargs) is None
    assert bias_correction(scores_for("temperature_2m", 1.5, n=29), **kwargs) is None
    assert bias_correction(scores_for("temperature_2m", 1.5, n=30), **kwargs) == -1.5
    thick = scores_for("temperature_2m", 1.5, n=50)
    assert thick[0].is_meaningful is True
    assert bias_correction(thick, **kwargs) == -1.5


def test_correction_only_for_matching_bucket_variable_provider_and_model() -> None:
    scores = scores_for("temperature_2m", 1.5, n=50)  # bucket 6-12 h
    base = {
        "variable": "temperature_2m",
        "provider": PROVIDER,
        "model_name": MODEL,
        "lead_time_s": 8 * H,
    }
    assert bias_correction(scores, **base) == -1.5
    assert bias_correction(scores, **{**base, "lead_time_s": 2 * H}) is None
    assert bias_correction(scores, **{**base, "lead_time_s": 6 * H}) == -1.5  # lower bound in
    assert bias_correction(scores, **{**base, "lead_time_s": 12 * H}) is None  # upper bound out
    assert bias_correction(scores, **{**base, "variable": "ghi"}) is None
    assert bias_correction(scores, **{**base, "provider": "bom"}) is None
    assert bias_correction(scores, **{**base, "model_name": "gfs"}) is None
    assert bias_correction(scores, **{**base, "model_name": None}) is None
    assert bias_correction([], **base) is None


# --------------------------------------------------------------------------- apply_bias_correction


def test_apply_corrects_and_records_provenance() -> None:
    scores = scores_for("temperature_2m", 1.5, n=214)
    series = forecast_series({"temperature_2m": [30.0, 31.0]})
    out = apply_bias_correction(series, scores)
    assert out is not series
    for i, expected in enumerate([28.5, 29.5]):
        item = out.steps[i].values["temperature_2m"]
        assert item.value == expected
        assert item.provenance.processing is not None
        assert "bias_corrected(-1.5 degC, n=214, ecmwf_ifs025, 6-12h)" in item.provenance.processing
        assert "nearest_grid_point" in item.provenance.processing  # history kept
        assert item.provenance.notes is not None and "raw_model_value=" in item.provenance.notes
        assert item.provenance.kind is DataKind.FORECAST
    # The raw series is untouched.
    assert series.steps[0].values["temperature_2m"].value == 30.0
    assert "bias_corrected" not in (
        series.steps[0].values["temperature_2m"].provenance.processing or ""
    )


def test_apply_leaves_values_without_meaningful_score_untouched() -> None:
    thin = scores_for("temperature_2m", 1.5, n=5)
    series = forecast_series({"temperature_2m": [30.0], "wind_speed_10m": [6.0]})
    out = apply_bias_correction(series, thin)
    assert out.steps[0].values["temperature_2m"] == series.steps[0].values["temperature_2m"]
    assert out.steps[0].values["wind_speed_10m"] == series.steps[0].values["wind_speed_10m"]


def test_apply_only_touches_matching_variable_and_lead_bucket() -> None:
    scores = scores_for("temperature_2m", 1.5, n=50, lead_h=8.0)  # 6-12 h only
    # steps at lead 8 h, 9 h, 14 h
    series = forecast_series({"temperature_2m": [30.0, 30.0, 30.0], "wind_speed_10m": [6.0] * 3})
    series = series.model_copy(update={"steps": series.steps})
    out = apply_bias_correction(series, scores)
    temps = [s.values["temperature_2m"].value for s in out.steps]
    assert temps == [28.5, 28.5, 28.5]  # 8, 9, 10 h are all in 6-12 h
    assert all(s.values["wind_speed_10m"].value == 6.0 for s in out.steps)
    late = forecast_series({"temperature_2m": [30.0]}, first_lead_h=14.0)
    assert apply_bias_correction(late, scores).steps[0].values["temperature_2m"].value == 30.0


def test_apply_without_known_lead_time_is_untouched() -> None:
    scores = scores_for("temperature_2m", 1.5, n=50)
    series = forecast_series({"temperature_2m": [30.0]}, init=None)
    out = apply_bias_correction(series, scores)
    assert out.steps[0].values["temperature_2m"].value == 30.0


def test_apply_never_corrects_observations() -> None:
    scores = scores_for("temperature_2m", 1.5, n=50)
    series = forecast_series({"temperature_2m": [30.0]})
    obs = series.model_copy(update={"kind": DataKind.OBSERVATION})
    out = apply_bias_correction(obs, scores)
    assert out.steps[0].values["temperature_2m"].value == 30.0


def test_apply_is_idempotent() -> None:
    scores = scores_for("temperature_2m", 1.5, n=50)
    once = apply_bias_correction(forecast_series({"temperature_2m": [30.0]}), scores)
    twice = apply_bias_correction(once, scores)
    assert twice.steps[0].values["temperature_2m"].value == 28.5
    assert twice.steps[0].values["temperature_2m"] == once.steps[0].values["temperature_2m"]


def test_irradiance_correction_never_negative_and_clip_is_noted() -> None:
    # Model over-forecasts GHI by 100 W/m2 on average -> correction -100.
    scores = scores_for("ghi", 100.0, n=60, base=300.0)
    series = forecast_series({"ghi": [5.0, 0.0, 450.0]}, first_lead_h=8.0)
    out = apply_bias_correction(series, scores)
    clipped = out.steps[0].values["ghi"]
    assert clipped.value == 0.0
    assert clipped.provenance.processing is not None
    assert "bias_corrected(-100 W/m2, n=60" in clipped.provenance.processing
    assert "clipped_to_registry_range" in clipped.provenance.processing
    # Night/zero irradiance is left exactly alone, with no spurious correction record.
    night = out.steps[1].values["ghi"]
    assert night.value == 0.0
    assert "bias_corrected" not in (night.provenance.processing or "")
    # A normal daytime value is corrected, unclipped.
    day = out.steps[2].values["ghi"]
    assert day.value == 350.0
    assert "clipped" not in (day.provenance.processing or "")


def test_positive_irradiance_correction_does_not_create_night_light() -> None:
    scores = scores_for("ghi", -80.0, n=60, base=300.0)  # model under-forecasts -> +80
    out = apply_bias_correction(forecast_series({"ghi": [0.0, 100.0]}), scores)
    assert out.steps[0].values["ghi"].value == 0.0
    assert out.steps[1].values["ghi"].value == 180.0


def test_corrected_values_stay_within_registry_ranges() -> None:
    cases = {
        "relative_humidity_2m": (-30.0, 90.0, 100.0),  # under-forecast humidity, +30 -> cap 100
        "wind_speed_10m": (10.0, 3.0, 0.0),  # over-forecast -> -10 -> floor 0
        "cloud_cover": (-50.0, 80.0, 100.0),
    }
    for variable, (bias, raw, expected) in cases.items():
        scores = scores_for(variable, bias, n=40, base=50.0)
        out = apply_bias_correction(forecast_series({variable: [raw]}), scores)
        item = out.steps[0].values[variable]
        spec = CANONICAL_VARIABLES[variable]
        assert item.value == expected
        assert spec.minimum <= item.value <= spec.maximum
        assert item.provenance.processing is not None
        assert "clipped_to_registry_range" in item.provenance.processing


def test_wind_direction_correction_wraps_around_north() -> None:
    # Model direction is 20 deg too high on average -> correction -20.
    pairs = [make_pair("wind_direction_10m", 100.0, 80.0, lead_h=8.0, k=k) for k in range(40)]
    scores = score_pairs(pairs, variable="wind_direction_10m", unit="deg", now=NOW)
    assert scores[0].bias == 20.0
    out = apply_bias_correction(forecast_series({"wind_direction_10m": [10.0, 200.0]}), scores)
    first = out.steps[0].values["wind_direction_10m"]
    assert first.value == 350.0  # 10 - 20 wraps to 350, not -10
    assert (
        first.provenance.processing is not None
        and "wrapped_to_0_360" in first.provenance.processing
    )
    assert out.steps[1].values["wind_direction_10m"].value == 180.0


def test_state_variables_are_never_corrected() -> None:
    pairs = [make_pair("weather_code", 3.0, 1.0, lead_h=8.0, k=k) for k in range(40)]
    scores = score_pairs(pairs, variable="weather_code", unit="wmo", now=NOW)
    out = apply_bias_correction(forecast_series({"weather_code": [3.0]}), scores)
    assert out.steps[0].values["weather_code"].value == 3.0


def test_provider_and_model_must_match_the_series() -> None:
    scores = scores_for("temperature_2m", 1.5, n=50)
    series = forecast_series({"temperature_2m": [30.0]})
    other = series.model_copy(update={"model_name": "gfs"})
    assert apply_bias_correction(other, scores).steps[0].values["temperature_2m"].value == 30.0
    other = series.model_copy(update={"provider": "bom"})
    assert apply_bias_correction(other, scores).steps[0].values["temperature_2m"].value == 30.0


def test_unusable_values_pass_through() -> None:
    scores = scores_for("temperature_2m", 1.5, n=50)
    series = forecast_series({"temperature_2m": [30.0]})
    step = series.steps[0]
    missing = measured("temperature_2m", None, step.values["temperature_2m"].provenance)
    series = series.model_copy(
        update={"steps": [step.model_copy(update={"values": {"temperature_2m": missing}})]}
    )
    out = apply_bias_correction(series, scores)
    assert out.steps[0].values["temperature_2m"].value is None


@pytest.mark.parametrize("n", [29, 30])
def test_threshold_edge_in_apply(n: int) -> None:
    scores = scores_for("temperature_2m", 2.0, n=n)
    out = apply_bias_correction(forecast_series({"temperature_2m": [30.0]}), scores)
    assert out.steps[0].values["temperature_2m"].value == (28.0 if n == 30 else 30.0)
