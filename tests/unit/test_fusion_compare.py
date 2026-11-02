"""Multi-model comparison: disagreement, alignment, thresholds, determinism. No network."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from rwi.contracts import VerificationScore, WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, QualityFlag, measured
from rwi.fusion import (
    DEFAULT_ALIGNMENT_TOLERANCE_S,
    DEFAULT_SIGNIFICANCE_THRESHOLDS,
    THRESHOLD_BASIS,
    compare_models,
    disagreements,
    resolve_threshold,
)

T0 = datetime(2027, 8, 24, 0, 0, tzinfo=UTC)
RETRIEVED = datetime(2027, 8, 23, 12, 0, tzinfo=UTC)
POINT = GeoPoint(latitude_deg=-23.698, longitude_deg=133.8807, elevation_m=545.0)


def make_series(
    model: str,
    values: Sequence[float | None],
    *,
    variable: str = "temperature_2m",
    step_s: float = 3600.0,
    start: datetime = T0,
    kind: DataKind = DataKind.FORECAST,
    point: GeoPoint = POINT,
    quality: QualityFlag = QualityFlag.OK,
    init: datetime | None = None,
) -> WeatherSeries:
    steps = []
    for i, v in enumerate(values):
        t = start + timedelta(seconds=i * step_s)
        prov = Provenance(
            provider="open-meteo",
            dataset="forecast",
            kind=kind,
            retrieved_at_utc=RETRIEVED,
            valid_time_utc=t,
            observation_time_utc=None,
            model_name=model,
            point=point,
        )
        steps.append(
            WeatherStep(
                valid_time_utc=t,
                values={variable: measured(variable, v, prov, quality=quality)},
            )
        )
    return WeatherSeries(
        point=point,
        requested_point=point,
        provider="open-meteo",
        dataset="forecast",
        kind=kind,
        model_name=model,
        model_init_time_utc=init,
        retrieved_at_utc=RETRIEVED,
        temporal_resolution_s=step_s,
        steps=steps,
    )


def test_three_models_agreeing_within_half_a_degree_are_not_significant() -> None:
    models = {
        "a": make_series("a", [20.0, 21.0, 22.0]),
        "b": make_series("b", [20.3, 21.2, 22.4]),
        "c": make_series("c", [19.9, 20.8, 22.1]),
    }
    cmp = compare_models(models, variable="temperature_2m")
    assert len(cmp.disagreements) == 3
    assert not any(d.is_significant for d in cmp.disagreements)
    assert all(d.spread < 0.5 for d in cmp.disagreements)
    assert all(d.threshold == 2.0 for d in cmp.disagreements)


def test_one_model_five_degrees_away_is_significant_with_correct_statistics() -> None:
    models = {
        "a": make_series("a", [20.0]),
        "b": make_series("b", [21.0]),
        "c": make_series("c", [26.0]),
    }
    (d,) = compare_models(models, variable="temperature_2m").disagreements
    assert d.is_significant
    assert d.spread == pytest.approx(6.0)
    assert d.mean == pytest.approx(67.0 / 3.0)
    assert d.median == pytest.approx(21.0)
    # population standard deviation (ddof=0)
    assert d.standard_deviation == pytest.approx(2.6246692913372702)
    assert d.values_by_model == {"a": 20.0, "b": 21.0, "c": 26.0}
    assert d.model_count == 3
    assert d.unit == "degC"
    assert d.valid_time_utc == T0
    assert d.provenance.kind is DataKind.DERIVED
    assert "engineering judgement" in d.provenance.notes  # type: ignore[operator]


def test_spread_exactly_at_threshold_is_not_significant() -> None:
    models = {"a": make_series("a", [20.0]), "b": make_series("b", [22.0])}
    (d,) = disagreements(models, variable="temperature_2m", threshold=2.0)
    assert d.spread == pytest.approx(2.0)
    assert not d.is_significant  # "exceeds", not "reaches"


def test_hourly_and_three_hourly_models_compared_only_at_shared_times() -> None:
    hourly = make_series("hourly", [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0])
    three_hourly = make_series("coarse", [10.5, 13.5, 16.5], step_s=10800.0)
    cmp = compare_models({"hourly": hourly, "coarse": three_hourly}, variable="temperature_2m")
    assert [d.valid_time_utc for d in cmp.disagreements] == [
        T0,
        T0 + timedelta(hours=3),
        T0 + timedelta(hours=6),
    ]
    for d in cmp.disagreements:
        assert set(d.values_by_model) == {"hourly", "coarse"}
        assert d.spread == pytest.approx(0.5)
    # The hours in between had a value from one model only; they were not interpolated.
    notes = " ".join(cmp.notes)
    assert "Nothing is interpolated" in notes
    assert "from only one model" in notes
    # each model's own series is kept as published
    assert len(cmp.series_by_model["hourly"]) == 7
    assert len(cmp.series_by_model["coarse"]) == 3


def test_model_missing_a_time_is_absent_from_that_point_not_substituted() -> None:
    a = make_series("a", [10.0, 11.0, 12.0])
    b = make_series("b", [10.5, None, 12.5])  # no usable value at T0+1h
    c = make_series("c", [11.0, 11.5, 13.0])
    cmp = compare_models({"a": a, "b": b, "c": c}, variable="temperature_2m")
    middle = cmp.disagreements[1]
    assert middle.valid_time_utc == T0 + timedelta(hours=1)
    assert set(middle.values_by_model) == {"a", "c"}
    assert middle.model_count == 2
    assert cmp.disagreements[0].model_count == 3
    assert "b at 1" in " ".join(cmp.notes)  # the absence is reported
    assert [t for t, _ in cmp.series_by_model["b"]] == [T0, T0 + timedelta(hours=2)]


def test_unusable_quality_value_is_not_a_value() -> None:
    a = make_series("a", [10.0])
    b = make_series("b", [30.0], quality=QualityFlag.OUT_OF_RANGE)
    cmp = compare_models({"a": a, "b": b}, variable="temperature_2m")
    assert cmp.disagreements == []
    assert cmp.series_by_model["b"] == []


def test_alignment_tolerance_matches_close_stamps_and_records_it() -> None:
    a = make_series("a", [10.0, 11.0])
    b = make_series("b", [12.0, 13.0], start=T0 + timedelta(minutes=4))
    cmp = compare_models({"a": a, "b": b}, variable="temperature_2m")
    assert len(cmp.disagreements) == 2
    assert cmp.disagreements[0].valid_time_utc == T0  # stamped with the earlier time
    assert cmp.disagreements[0].spread == pytest.approx(2.0)
    assert "differ" in " ".join(cmp.notes)
    assert "240" in " ".join(cmp.notes)  # the largest offset, in seconds


def test_stamps_outside_tolerance_are_not_matched() -> None:
    a = make_series("a", [10.0])
    b = make_series("b", [12.0], start=T0 + timedelta(seconds=DEFAULT_ALIGNMENT_TOLERANCE_S + 60))
    cmp = compare_models({"a": a, "b": b}, variable="temperature_2m")
    assert cmp.disagreements == []
    assert "not comparable" in " ".join(cmp.notes)
    # a wider tolerance, explicitly chosen, does match them
    wide = compare_models({"a": a, "b": b}, variable="temperature_2m", tolerance_s=600.0)
    assert len(wide.disagreements) == 1


def test_single_model_has_no_disagreement_and_says_it_is_not_agreement() -> None:
    cmp = compare_models({"only": make_series("only", [20.0, 21.0])}, variable="temperature_2m")
    assert cmp.disagreements == []
    assert len(cmp.series_by_model["only"]) == 2
    text = " ".join(cmp.notes)
    assert "cannot be compared" in text
    assert "NOT evidence" in text
    assert (
        disagreements(
            {"only": make_series("only", [20.0])}, variable="temperature_2m", threshold=2.0
        )
        == []
    )


def test_two_models_that_agree_exactly_have_a_real_zero_spread() -> None:
    """Agreement (spread 0.0, a comparison) differs from no comparison (empty list)."""
    models = {"a": make_series("a", [20.0]), "b": make_series("b", [20.0])}
    (d,) = compare_models(models, variable="temperature_2m").disagreements
    assert d.spread == 0.0
    assert d.model_count == 2
    assert not d.is_significant


def test_model_with_no_data_is_listed_but_does_not_count() -> None:
    models = {
        "a": make_series("a", [20.0]),
        "b": make_series("b", [21.0]),
        "empty": make_series("empty", []),
    }
    cmp = compare_models(models, variable="temperature_2m")
    assert cmp.series_by_model["empty"] == []
    assert cmp.disagreements[0].model_count == 2
    assert "empty" in " ".join(cmp.notes)


def test_native_step_of_interpolated_model_is_called_out() -> None:
    a = make_series("ecmwf_ifs025", [10.0, 10.5, 11.0, 11.5])
    b = make_series("ecmwf_ifs", [10.2, 10.6, 11.1, 11.9])
    notes = " ".join(
        compare_models({"ecmwf_ifs025": a, "ecmwf_ifs": b}, variable="temperature_2m").notes
    )
    assert "ecmwf_ifs025: native step is 3 h" in notes
    assert "ecmwf_ifs:" not in notes.replace("ecmwf_ifs025:", "")


def test_different_grid_points_and_runs_are_noted() -> None:
    other = GeoPoint(latitude_deg=-23.5, longitude_deg=133.75, elevation_m=545.0)
    a = make_series("a", [10.0], init=T0 - timedelta(hours=6))
    b = make_series("b", [11.0], point=other, init=T0 - timedelta(hours=12))
    text = " ".join(compare_models({"a": a, "b": b}, variable="temperature_2m").notes)
    assert "different grid points" in text
    assert "different runs" in text


def test_points_too_far_apart_are_refused() -> None:
    far = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456)
    with pytest.raises(ValueError, match="not the same point"):
        compare_models(
            {"a": make_series("a", [1.0]), "b": make_series("b", [1.0], point=far)},
            variable="temperature_2m",
        )


def test_mixed_or_non_model_kinds_are_refused() -> None:
    with pytest.raises(ValueError, match="only model output"):
        compare_models(
            {
                "a": make_series("a", [1.0], kind=DataKind.CLEAR_SKY_MODEL),
                "b": make_series("b", [1.0], kind=DataKind.CLEAR_SKY_MODEL),
            },
            variable="temperature_2m",
        )
    with pytest.raises(ValueError, match="share one data kind"):
        compare_models(
            {
                "a": make_series("a", [1.0]),
                "b": make_series("b", [1.0], kind=DataKind.ANALYSIS),
            },
            variable="temperature_2m",
        )


def test_wind_direction_spread_is_circular() -> None:
    models = {
        "a": make_series("a", [350.0], variable="wind_direction_10m"),
        "b": make_series("b", [10.0], variable="wind_direction_10m"),
    }
    (d,) = compare_models(models, variable="wind_direction_10m").disagreements
    assert d.spread == pytest.approx(20.0)  # not 340
    assert d.mean == pytest.approx(0.0, abs=1e-9) or d.mean == pytest.approx(360.0)
    assert not d.is_significant
    wide = {
        "a": make_series("a", [10.0], variable="wind_direction_10m"),
        "b": make_series("b", [100.0], variable="wind_direction_10m"),
    }
    (w,) = compare_models(wide, variable="wind_direction_10m").disagreements
    assert w.spread == pytest.approx(90.0)
    assert w.is_significant  # 90 > 45


# --- thresholds ---------------------------------------------------------------------------


def test_default_thresholds_cover_the_core_variables_with_justification() -> None:
    for name in ("temperature_2m", "wind_speed_10m", "cloud_cover", "ghi"):
        assert name in DEFAULT_SIGNIFICANCE_THRESHOLDS
        assert THRESHOLD_BASIS[name].value == DEFAULT_SIGNIFICANCE_THRESHOLDS[name]
        assert len(THRESHOLD_BASIS[name].justification) > 80
        assert "judgement" in THRESHOLD_BASIS[name].status
    assert DEFAULT_SIGNIFICANCE_THRESHOLDS["temperature_2m"] == 2.0
    assert DEFAULT_SIGNIFICANCE_THRESHOLDS["wind_speed_10m"] == 3.0
    assert DEFAULT_SIGNIFICANCE_THRESHOLDS["cloud_cover"] == 30.0
    assert DEFAULT_SIGNIFICANCE_THRESHOLDS["ghi"] == 150.0
    assert set(THRESHOLD_BASIS) == set(DEFAULT_SIGNIFICANCE_THRESHOLDS)


def test_default_thresholds_cannot_be_mutated_by_callers() -> None:
    with pytest.raises(TypeError):
        DEFAULT_SIGNIFICANCE_THRESHOLDS["ghi"] = 1.0  # type: ignore[index]


def test_thresholds_are_overridable() -> None:
    models = {"a": make_series("a", [20.0]), "b": make_series("b", [21.5])}
    default = compare_models(models, variable="temperature_2m")
    tight = compare_models(
        models, variable="temperature_2m", significance_thresholds={"temperature_2m": 1.0}
    )
    assert not default.disagreements[0].is_significant
    assert tight.disagreements[0].is_significant
    assert tight.disagreements[0].threshold == 1.0
    assert "caller supplied" in " ".join(tight.notes)
    assert resolve_threshold("ghi", {"ghi": 99.0}) == 99.0
    assert resolve_threshold("ghi", {"cloud_cover": 5.0}) == 150.0  # other keys leave it alone


def test_variable_without_a_threshold_needs_one_rather_than_inventing_it() -> None:
    models = {
        "a": make_series("a", [100000.0], variable="pressure_msl"),
        "b": make_series("b", [100100.0], variable="pressure_msl"),
    }
    with pytest.raises(ValueError, match="no significance threshold"):
        compare_models(models, variable="pressure_msl")
    ok = compare_models(
        models, variable="pressure_msl", significance_thresholds={"pressure_msl": 50}
    )
    assert ok.disagreements[0].is_significant


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_invalid_thresholds_are_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match="threshold"):
        resolve_threshold("ghi", {"ghi": bad})
    with pytest.raises(ValueError, match="threshold"):
        disagreements({"a": make_series("a", [1.0])}, variable="temperature_2m", threshold=bad)


def test_empty_input_and_unknown_variable_are_errors() -> None:
    with pytest.raises(ValueError, match="empty"):
        compare_models({}, variable="temperature_2m")
    with pytest.raises(Exception, match="not a canonical variable"):
        compare_models({"a": make_series("a", [1.0])}, variable="not_a_variable")


# --- preference wiring ---------------------------------------------------------------------


def test_comparison_has_no_preferred_model_by_default() -> None:
    models = {"a": make_series("a", [20.0]), "b": make_series("b", [23.0])}
    cmp = compare_models(models, variable="temperature_2m")
    assert cmp.preferred_model is None
    assert cmp.preference_basis is not None
    assert "no model has earned preference" in cmp.preference_basis


def _score(
    model: str = "ecmwf_ifs", *, samples: int = 200, skill: float = 0.25
) -> VerificationScore:
    return VerificationScore(
        variable="temperature_2m",
        provider="open-meteo",
        model_name=model,
        lead_time_bucket_s=(0.0, 86400.0),
        sample_count=samples,
        mean_absolute_error=1.2,
        root_mean_square_error=1.5,
        skill_vs_persistence=skill,
        unit="degC",
        provenance=Provenance(
            provider="rwi",
            dataset="verification",
            kind=DataKind.DERIVED,
            retrieved_at_utc=RETRIEVED,
        ),
    )


def test_compare_models_reports_earned_preference_only_when_given_the_evidence() -> None:
    models = {"ecmwf_ifs": make_series("ecmwf_ifs", [20.0]), "gfs": make_series("gfs", [23.0])}
    none = compare_models(
        models,
        variable="temperature_2m",
        verification_scores=[_score(samples=5)],
        lead_time_s=6 * 3600.0,
    )
    assert none.preferred_model is None
    assert "5 sample" in (none.preference_basis or "")
    earned = compare_models(
        models, variable="temperature_2m", verification_scores=[_score()], lead_time_s=6 * 3600.0
    )
    assert earned.preferred_model == "ecmwf_ifs"
    absent = compare_models(
        models,
        variable="temperature_2m",
        verification_scores=[_score("icon_global")],
        lead_time_s=6 * 3600.0,
    )
    assert absent.preferred_model is None
    assert "not among the compared models" in (absent.preference_basis or "")


# --- determinism ---------------------------------------------------------------------------


def test_input_dict_order_never_changes_output() -> None:
    series = {
        "zeta": make_series("zeta", [20.0, 21.0, 22.0]),
        "alpha": make_series("alpha", [23.0, 21.5, 22.5]),
        "mid": make_series("mid", [20.5, 24.0, 22.0], step_s=3600.0),
    }
    forward = compare_models(series, variable="temperature_2m", now=RETRIEVED)
    backward = compare_models(
        dict(reversed(list(series.items()))), variable="temperature_2m", now=RETRIEVED
    )
    assert forward == backward
    assert list(forward.series_by_model) == ["alpha", "mid", "zeta"]
    for d in forward.disagreements:
        assert list(d.values_by_model) == ["alpha", "mid", "zeta"]
    d_fwd = disagreements(series, variable="temperature_2m", threshold=2.0)
    d_bwd = disagreements(
        dict(reversed(list(series.items()))), variable="temperature_2m", threshold=2.0
    )
    assert d_fwd == d_bwd
