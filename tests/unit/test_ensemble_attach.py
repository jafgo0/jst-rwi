"""Tests for rwi.ensemble.attach."""

from __future__ import annotations

import struct
from datetime import timedelta

import pytest
from test_ensemble_support import (
    POINT,
    all_measured,
    deterministic,
    ensemble,
    hour,
    step,
)

from rwi.contracts import WeatherSeries
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Uncertainty
from rwi.ensemble import attach_uncertainty, spread_skill_note, summarise


def _ens_summaries(hours: tuple[int, ...] = (0, 1, 2)):  # type: ignore[no-untyped-def]
    cols = [{h: 100.0 * (i + 1) + h for h in hours} for i in range(5)]
    return summarise(ensemble("ghi", cols), variable="ghi")


def _series() -> WeatherSeries:
    base = deterministic("ghi", {0: 300.5, 1: 410.25, 2: 0.1 + 0.2, 3: 999.0})
    # add a second variable and a missing value to be sure nothing else is disturbed
    steps = [
        step(s.valid_time_utc, ghi=s.values["ghi"].value, temperature_2m=25.0 + i)
        for i, s in enumerate(base.steps)
    ]
    steps[1] = step(hour(1), ghi=None, temperature_2m=26.0)
    return base.model_copy(update={"steps": steps})


def test_values_bit_identical_and_input_not_mutated() -> None:
    series = _series()
    before = [(t, k, m.value, m.unit, m.quality, m.provenance) for t, k, m in all_measured(series)]
    snapshot = series.model_dump()
    out = attach_uncertainty(series, _ens_summaries())
    assert out is not series
    assert series.model_dump() == snapshot  # input untouched
    after = [(t, k, m.value, m.unit, m.quality, m.provenance) for t, k, m in all_measured(out)]
    assert len(before) == len(after)
    for b, a in zip(before, after, strict=True):
        assert b == a
        if b[2] is not None:
            assert a[2] is not None
            assert struct.pack("<d", b[2]) == struct.pack("<d", a[2])  # bit pattern
        else:
            assert a[2] is None
    # structure unchanged
    assert out.times == series.times
    assert out.model_name == series.model_name
    assert out.kind is series.kind


def test_attaches_where_summary_matches_and_none_elsewhere() -> None:
    summaries = _ens_summaries((0, 2))  # no summary for hour 1 or 3
    out = attach_uncertainty(_series(), summaries)
    by_hour = {s.valid_time_utc: s for s in out.steps}
    u0 = by_hour[hour(0)].values["ghi"].uncertainty
    assert isinstance(u0, Uncertainty)
    assert u0 == summaries[0].as_uncertainty()
    assert u0.interval_probability == pytest.approx(0.8)
    assert u0.method is not None and "ensemble_spread" in u0.method
    assert by_hour[hour(2)].values["ghi"].uncertainty == summaries[1].as_uncertainty()
    assert by_hour[hour(1)].values["ghi"].uncertainty is None  # missing value stays bare
    assert by_hour[hour(3)].values["ghi"].uncertainty is None  # no summary
    # a variable with no summaries is untouched
    assert all(s.values["temperature_2m"].uncertainty is None for s in out.steps)


def test_series_without_any_matching_summary_is_unchanged_in_content() -> None:
    out = attach_uncertainty(_series(), [])
    assert out.model_dump() == _series().model_dump()


def test_time_tolerance_is_respected() -> None:
    summaries = _ens_summaries((0,))
    series = deterministic("ghi", {0: 1.0})
    shifted = series.model_copy(
        update={"steps": [step(hour(0) + timedelta(minutes=40), ghi=1.0)]}
    )
    assert attach_uncertainty(shifted, summaries).steps[0].values["ghi"].uncertainty is None
    ok = attach_uncertainty(shifted, summaries, tolerance_s=3000.0)
    assert ok.steps[0].values["ghi"].uncertainty is not None


def test_nearest_summary_wins() -> None:
    summaries = _ens_summaries((0, 1))
    series = deterministic("ghi", {0: 1.0}).model_copy(
        update={"steps": [step(hour(0) + timedelta(minutes=20), ghi=1.0)]}
    )
    u = attach_uncertainty(series, summaries).steps[0].values["ghi"].uncertainty
    assert u == summaries[0].as_uncertainty()


def test_summary_for_a_distant_place_is_not_used() -> None:
    summaries = _ens_summaries((0,))
    far = GeoPoint(latitude_deg=-34.9285, longitude_deg=138.6007, elevation_m=50.0)
    series = deterministic("ghi", {0: 1.0}).model_copy(update={"point": far})
    out = attach_uncertainty(series, summaries)
    assert out.steps[0].values["ghi"].uncertainty is None
    assert POINT != far


def test_existing_uncertainty_kept_unless_overwrite() -> None:
    summaries = _ens_summaries((0,))
    base = deterministic("ghi", {0: 5.0})
    existing = Uncertainty(standard_deviation=1.0, method="verified_mae")
    m = base.steps[0].values["ghi"].model_copy(update={"uncertainty": existing})
    new_step = base.steps[0].model_copy(update={"values": {"ghi": m}})
    series = base.model_copy(update={"steps": [new_step]})
    kept = attach_uncertainty(series, summaries).steps[0].values["ghi"]
    assert kept.uncertainty == existing
    replaced = attach_uncertainty(series, summaries, overwrite=True).steps[0].values["ghi"]
    assert replaced.uncertainty == summaries[0].as_uncertainty()
    assert replaced.value == 5.0


def test_only_forecast_series_accepted() -> None:
    obs = deterministic("ghi", {0: 5.0}).model_copy(update={"kind": DataKind.OBSERVATION})
    with pytest.raises(ValueError, match="FORECAST"):
        attach_uncertainty(obs, _ens_summaries((0,)))


def test_as_uncertainty_is_p10_p90_with_probability_point_eight() -> None:
    summaries = summarise(
        ensemble("ghi", [{0: float(v)} for v in range(31)]), variable="ghi"
    )
    (s,) = summaries
    u = s.as_uncertainty()
    assert u.lower == s.percentiles["10"] == 3.0
    assert u.upper == s.percentiles["90"] == 27.0
    assert u.interval_probability == 0.8


def test_spread_skill_note_content() -> None:
    (s,) = summarise(
        ensemble("ghi", [{0: float(v)} for v in range(31)]), variable="ghi"
    )
    note = spread_skill_note(s)
    assert note.count("\n") == 0
    assert "31" in note and "W/m2" in note
    assert "10th-90th percentile 3 to 27" in note
    assert "not been verified" in note
    assert "reliable" in note
