"""ADR 0008 §6: strategy runs against P10/P50/P90 generation bands."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance, Uncertainty
from rwi.strategy.scenarios import evaluate_scenario, scenario_sensitivity
from rwi.strategy.segments import (
    SegmentConditions,
    generation_band_from_ghi,
    ghi_band_ratios,
    scale_generation,
    segments_from_distance,
    select_generation_band,
)
from rwi.vehicle.dynamics import DEFAULT_VEHICLE

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
T = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)


def _ghi(value: float, lower: float | None, upper: float | None) -> Measured:
    prov = Provenance(
        provider="rwi-ml", dataset="postprocessed", kind=DataKind.FORECAST,
        retrieved_at_utc=T, valid_time_utc=T,
        point=GeoPoint(latitude_deg=-20.0, longitude_deg=134.0),
    )
    unc = (
        Uncertainty(lower=lower, upper=upper, interval_probability=0.8,
                    method="quantile_regression:lightgbm")
        if lower is not None and upper is not None else None
    )
    return Measured(variable="ghi", value=value, unit="W/m2", provenance=prov, uncertainty=unc)


def _banded(n: int = 6) -> list[SegmentConditions]:
    segs = segments_from_distance(120_000.0, segment_count=n, generation_w=800.0)
    return generation_band_from_ghi(segs, [_ghi(600.0, 300.0, 750.0)] * n)


def _run(segments: list[SegmentConditions], **kw: object):
    return evaluate_scenario(
        name="c", description="c", target_speed_kmh=70.0, vehicle=DEFAULT_VEHICLE,
        start_soc_fraction=0.6, segments=segments, now_utc=NOW, **kw,
    )


def test_band_must_bracket_p50_and_come_in_pairs() -> None:
    with pytest.raises(ValidationError):
        SegmentConditions(distance_m=1.0, generation_w=500.0, generation_p10_w=600.0,
                          generation_p90_w=700.0)
    with pytest.raises(ValidationError):
        SegmentConditions(distance_m=1.0, generation_w=500.0, generation_p10_w=400.0)
    with pytest.raises(ValidationError):
        SegmentConditions(distance_m=1.0, generation_p10_w=400.0, generation_p90_w=600.0)


def test_ratios_never_invent_a_band() -> None:
    lo, hi = ghi_band_ratios([
        _ghi(600.0, 300.0, 750.0), _ghi(0.0, 0.0, 0.0), _ghi(500.0, None, None), None,
    ])
    assert lo == [0.5, 1.0, 1.0, 1.0]
    assert hi == [1.25, 1.0, 1.0, 1.0]


def test_band_scales_from_ghi_and_selects() -> None:
    segs = _banded()
    assert all(s.has_generation_band for s in segs)
    assert segs[0].generation_p10_w == pytest.approx(400.0)
    assert segs[0].generation_p90_w == pytest.approx(1000.0)
    p10 = select_generation_band(segs, "p10")
    assert p10[0].generation_w == pytest.approx(400.0) and not p10[0].has_generation_band
    doubled = scale_generation(segs, 2.0)
    assert doubled[0].generation_p10_w == pytest.approx(800.0)


def test_p10_plan_ends_lower_than_p50_and_sweep_uses_the_band() -> None:
    segs = _banded()
    p50 = _run(segs)
    p10 = _run(segs, generation_band="p10")
    assert p10.energy_generated_wh < p50.energy_generated_wh
    assert p10.end_soc_fraction < p50.end_soc_fraction
    # The sweep runs from the band even at f = 0 and its pessimistic case IS the P10 run.
    assert p50.assumptions["pessimistic_net_energy_wh"] == pytest.approx(p10.net_energy_wh)
    assert p50.assumptions["generation_band"] == "p50"
    assert p50.assumptions["generation_band_segments"] == 6.0
    assert any("P10" in n for n in p50.uncertainty_notes)


def test_without_bands_behaviour_is_unchanged() -> None:
    plain = segments_from_distance(120_000.0, segment_count=6, generation_w=800.0)
    a = _run(plain)
    b = _run(plain, generation_band="p10")
    assert a.end_soc_fraction == b.end_soc_fraction
    assert "pessimistic_feasible" not in a.assumptions
    assert any("no segment carries an ML generation band" in n for n in b.uncertainty_notes)


def test_scenario_sensitivity_prefers_band_over_fraction() -> None:
    sens = scenario_sensitivity(
        uncertainty_fraction=0.9, segments=_banded(), name="c", description="c",
        target_speed_kmh=70.0, vehicle=DEFAULT_VEHICLE, start_soc_fraction=0.6, now_utc=NOW,
    )
    # P10 = 0.5 x P50, far milder than the (1 - 0.9) scaling would be.
    p10 = _run(_banded(), generation_band="p10")
    assert sens.pessimistic.energy_generated_wh == pytest.approx(p10.energy_generated_wh)
