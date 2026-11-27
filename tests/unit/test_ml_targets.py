"""Truth hierarchy and derived wind u/v targets (ADR 0011 §2-3)."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from rwi.contracts import ForecastObservationPair, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, measured
from rwi.core.wind import met_to_uv
from rwi.ml.__main__ import _target_kind
from rwi.ml.quantile import MlUnavailableError
from rwi.ml.targets import (
    BOM_PROVIDER,
    ONBOARD_PROVIDER,
    TruthPairs,
    latest_cutoff,
    target_kind_for,
    truth_pairs,
    truth_priority,
    upstream_value,
    uv_pairs,
)

T = datetime(2027, 8, 22, 3, 0, tzinfo=UTC)
GRID = GeoPoint(latitude_deg=-16.25, longitude_deg=133.37)
STATION = GeoPoint(latitude_deg=-16.30, longitude_deg=133.40)


def _pair(
    variable: str, fc: float, obs: float, *, hour: int = 0, obs_point: GeoPoint = STATION
) -> ForecastObservationPair:
    valid = T + timedelta(hours=hour)
    return ForecastObservationPair(
        variable=variable,
        valid_time_utc=valid,
        forecast_value=fc,
        observed_value=obs,
        lead_time_s=6 * 3600.0 + hour * 3600.0,
        provider="open-meteo",
        model_name="best_match",
        forecast_point=GRID,
        observation_point=obs_point,
        separation_m=0.0 if obs_point == GRID else 6000.0,
        time_offset_s=0.0,
        forecast_recorded_at_utc=T - timedelta(hours=5),
    )


class FakeStore:
    """`PairSource` keyed by (variable, observation provider)."""

    def __init__(self, rows: dict[tuple[str, str], list[ForecastObservationPair]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str | None]] = []

    def pairs(
        self, *, variable: str, observation_provider: str | None = None
    ) -> list[ForecastObservationPair]:
        self.calls.append((variable, observation_provider))
        assert observation_provider is not None, "truth_pairs must always name its source"
        return list(self.rows.get((variable, observation_provider), []))


# --------------------------------------------------------------------------
# priority


def test_priority_per_variable() -> None:
    assert truth_priority("ghi") == (ONBOARD_PROVIDER, BOM_PROVIDER)
    assert truth_priority("temperature_2m") == (ONBOARD_PROVIDER, BOM_PROVIDER)
    # Airmar wind is sensor height, never 10 m truth.
    for wind in ("wind_speed_10m", "wind_u_10m", "wind_v_10m"):
        assert truth_priority(wind) == (BOM_PROVIDER,)
    assert truth_priority("cloud_cover") == (BOM_PROVIDER,)


def test_onboard_wins_and_bom_fills_the_rest() -> None:
    store = FakeStore(
        {
            ("ghi", ONBOARD_PROVIDER): [_pair("ghi", 600.0, 590.0, hour=1, obs_point=GRID)],
            ("ghi", BOM_PROVIDER): [
                _pair("ghi", 500.0, 480.0, hour=0),
                _pair("ghi", 600.0, 560.0, hour=1),  # same forecast value: loses to ML-01
            ],
        }
    )
    truth = truth_pairs(store, "ghi")
    assert [p.observed_value for p in truth.pairs] == [480.0, 590.0]
    assert truth.pair_sources == (BOM_PROVIDER, ONBOARD_PROVIDER)
    assert truth.sources == {BOM_PROVIDER: 1, ONBOARD_PROVIDER: 1}
    assert truth.target_kind == "telemetry+bom_observation"
    assert store.calls == [("ghi", ONBOARD_PROVIDER), ("ghi", BOM_PROVIDER)]


def test_target_kind_for_each_mix() -> None:
    assert target_kind_for({ONBOARD_PROVIDER: 3}) == "telemetry"
    assert target_kind_for({BOM_PROVIDER: 3, ONBOARD_PROVIDER: 0}) == "bom_observation"
    assert target_kind_for({BOM_PROVIDER: 1, ONBOARD_PROVIDER: 2}) == "telemetry+bom_observation"
    with pytest.raises(ValueError, match="no truth rows"):
        target_kind_for({})


def test_known_at_keeps_only_training_rows() -> None:
    truth = TruthPairs(
        "ghi",
        (_pair("ghi", 1.0, 1.0, hour=0), _pair("ghi", 1.0, 1.0, hour=3)),
        (ONBOARD_PROVIDER, BOM_PROVIDER),
    )
    train = truth.known_at(T + timedelta(hours=1))
    assert train.pair_sources == (ONBOARD_PROVIDER,)
    assert train.target_kind == "telemetry"
    assert truth.known_at(T - timedelta(days=1)).pairs == ()


# --------------------------------------------------------------------------
# u/v


def test_uv_pairs_match_met_to_uv() -> None:
    u, v = uv_pairs([_pair("wind_speed_10m", 8.0, 6.0)], [_pair("wind_direction_10m", 90.0, 135.0)])
    [pu], [pv] = u, v
    assert (pu.variable, pv.variable) == ("wind_u_10m", "wind_v_10m")
    fu, fv = met_to_uv(8.0, 90.0)
    ou, ov = met_to_uv(6.0, 135.0)
    assert (pu.forecast_value, pv.forecast_value) == pytest.approx((fu, fv))
    assert (pu.observed_value, pv.observed_value) == pytest.approx((ou, ov))
    # Wind from the east blows toward the west: u negative, v ~ 0.
    assert pu.forecast_value == pytest.approx(-8.0)
    assert pv.forecast_value == pytest.approx(0.0, abs=1e-9)
    assert pu.valid_time_utc == T and pu.forecast_point == GRID


def test_calm_gives_zero_vector() -> None:
    [pu], [pv] = uv_pairs(
        [_pair("wind_speed_10m", 0.0, 0.0)], [_pair("wind_direction_10m", 270.0, 10.0)]
    )
    assert (pu.forecast_value, pv.forecast_value) == pytest.approx((0.0, 0.0), abs=1e-12)
    assert (pu.observed_value, pv.observed_value) == pytest.approx((0.0, 0.0), abs=1e-12)


def test_unmatched_and_non_finite_rows_are_dropped() -> None:
    speed = [
        _pair("wind_speed_10m", 5.0, 4.0, hour=0),
        _pair("wind_speed_10m", 5.0, 4.0, hour=1),  # no direction at this time
        _pair("wind_speed_10m", 5.0, math.nan, hour=2),
        _pair("wind_speed_10m", 5.0, 4.0, hour=3, obs_point=GRID),  # other station
    ]
    direction = [
        _pair("wind_direction_10m", 180.0, 170.0, hour=0),
        _pair("wind_direction_10m", 180.0, 170.0, hour=2),
        _pair("wind_direction_10m", 180.0, 170.0, hour=3),
    ]
    u, v = uv_pairs(speed, direction)
    assert [p.valid_time_utc for p in u] == [T]
    assert len(v) == 1


def test_truth_pairs_for_u_uses_bom_speed_and_direction() -> None:
    store = FakeStore(
        {
            ("wind_speed_10m", BOM_PROVIDER): [_pair("wind_speed_10m", 10.0, 9.0)],
            ("wind_direction_10m", BOM_PROVIDER): [_pair("wind_direction_10m", 0.0, 0.0)],
        }
    )
    truth = truth_pairs(store, "wind_v_10m")
    [p] = truth.pairs
    assert p.variable == "wind_v_10m"
    assert (p.forecast_value, p.observed_value) == pytest.approx((-10.0, -9.0))
    assert truth.target_kind == "bom_observation"
    assert ("wind_speed_10m", ONBOARD_PROVIDER) not in store.calls


def _step(**values: float) -> WeatherStep:
    prov = Provenance(
        provider="open-meteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        retrieved_at_utc=T - timedelta(hours=1),
        valid_time_utc=T,
        model_name="best_match",
        model_init_time_utc=T - timedelta(hours=2),
        point=GRID,
    )
    return WeatherStep(
        valid_time_utc=T, values={k: measured(k, x, prov) for k, x in values.items()}
    )


def test_upstream_value_derives_uv_at_inference() -> None:
    step = _step(wind_speed_10m=5.0, wind_direction_10m=0.0, ghi=700.0)
    assert upstream_value(step, "ghi") == 700.0
    assert upstream_value(step, "wind_u_10m") == pytest.approx(0.0, abs=1e-12)
    assert upstream_value(step, "wind_v_10m") == pytest.approx(-5.0)
    assert upstream_value(_step(wind_speed_10m=5.0), "wind_u_10m") is None


def test_latest_cutoff_is_the_later_one() -> None:
    a, b = T - timedelta(days=2), T - timedelta(days=1)
    assert latest_cutoff(a, b) == b


# --------------------------------------------------------------------------
# CLI: target kind follows the training truth


def test_cli_target_kind_is_derived_and_checked() -> None:
    truth = TruthPairs(
        "ghi",
        (_pair("ghi", 1.0, 1.0, hour=0), _pair("ghi", 1.0, 1.0, hour=5)),
        (BOM_PROVIDER, ONBOARD_PROVIDER),
    )
    cutoff = T + timedelta(hours=1)  # only the BoM row is training data
    assert _target_kind(truth, cutoff, None) == "bom_observation"
    assert _target_kind(truth, cutoff, "bom_observation") == "bom_observation"
    with pytest.raises(MlUnavailableError, match="contradicts"):
        _target_kind(truth, cutoff, "telemetry")
    with pytest.raises(MlUnavailableError, match="no truth rows"):
        _target_kind(truth, T - timedelta(days=1), None)
