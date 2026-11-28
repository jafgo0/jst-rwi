"""Clear-sky check against 1-minute data and hourly bias statistics (ADR 0017)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance, QualityFlag
from rwi.solar import clear_sky_series
from rwi.validation.truth import bias_statistics, clear_sky_check

ASP = GeoPoint(latitude_deg=-23.798, longitude_deg=133.888, elevation_m=547.0)
CELL = GeoPoint(latitude_deg=-24.0, longitude_deg=133.75)
NOW = datetime(2026, 11, 1, tzinfo=UTC)
DAY = datetime(2024, 8, 22, tzinfo=UTC)


def _minutes(scale: float = 1.0, cloud: tuple[int, int] | None = None) -> pd.DataFrame:
    """One UTC day of 1-minute GHI/DNI equal to the clear-sky model times ``scale``."""
    index = pd.date_range(DAY, periods=1440, freq="1min")
    model = clear_sky_series(ASP, [(t + pd.Timedelta(seconds=30)).to_pydatetime() for t in index])
    ghi = np.array([c.ghi_w_m2 for c in model]) * scale
    dni = np.array([c.dni_w_m2 for c in model]) * scale
    if cloud is not None:
        rng = np.random.default_rng(7)
        a, b = cloud
        ghi[a:b] *= rng.uniform(0.2, 1.0, b - a)
    return pd.DataFrame({"ghi": ghi, "dni": dni}, index=index)


def test_model_against_itself_is_clear_and_unbiased() -> None:
    check = clear_sky_check(_minutes(), ASP)
    assert check.n_minutes == 1440
    assert 0 < check.n_clear_minutes <= check.n_lit_minutes < 1440
    assert check.n_clear_minutes > 0.9 * check.n_lit_minutes
    assert check.ghi is not None and check.ghi.mean_bias == pytest.approx(0.0, abs=1e-9)
    assert check.ghi.energy_ratio == pytest.approx(1.0)
    assert check.dni is not None and check.dni.rmse == pytest.approx(0.0, abs=1e-9)
    assert check.provenance.kind is DataKind.DERIVED
    assert "monthly Linke" in check.linke_turbidity_note
    assert check.last_utc == DAY + timedelta(days=1)


def test_dimmer_sky_shows_up_as_a_model_overestimate() -> None:
    check = clear_sky_check(_minutes(scale=0.95), ASP)
    assert check.ghi is not None and check.ghi.energy_ratio == pytest.approx(1 / 0.95)
    assert check.ghi.mean_bias > 0


def test_cloudy_minutes_are_not_clear() -> None:
    clear = clear_sky_check(_minutes(), ASP).n_clear_minutes
    # 02:00-04:00 UTC is around local noon at Alice Springs.
    broken = clear_sky_check(_minutes(cloud=(120, 240)), ASP).n_clear_minutes
    assert broken < clear - 100


def test_missing_minutes_and_index_gaps_are_not_counted() -> None:
    m = _minutes()
    full = clear_sky_check(m, ASP).n_clear_minutes
    m.loc[m.index[150:160], "ghi"] = np.nan
    gappy = m.drop(m.index[200:210])
    check = clear_sky_check(gappy, ASP)
    assert check.n_minutes == 1440
    assert check.n_clear_minutes <= full - 20


def test_turbidity_note_is_carried() -> None:
    check = clear_sky_check(_minutes(), ASP, linke_turbidity=3.0, linke_turbidity_note="TL 3.0")
    assert check.linke_turbidity_note == "TL 3.0"
    assert "TL 3.0" in (check.provenance.processing or "")


def _series(
    kind: DataKind, point: GeoPoint, values: dict[datetime, float | None], **kw: object
) -> WeatherSeries:
    steps = []
    for t, v in sorted(values.items()):
        prov = Provenance(
            provider="test",
            dataset="d",
            kind=kind,
            retrieved_at_utc=NOW,
            valid_time_utc=t,
            observation_time_utc=t if kind is DataKind.OBSERVATION else None,
            point=point,
        )
        flag = QualityFlag.OK if v is not None else QualityFlag.MISSING
        m = Measured(variable="ghi", value=v, unit="W/m2", provenance=prov, quality=flag)
        steps.append(WeatherStep(valid_time_utc=t, values={"ghi": m}))
    args: dict[str, object] = {
        "point": point,
        "provider": "test",
        "dataset": "d",
        "kind": kind,
        "retrieved_at_utc": NOW,
        "temporal_resolution_s": 3600.0,
        "steps": steps,
        **kw,
    }
    return WeatherSeries.model_validate(args)


def _h(hour: int) -> datetime:
    return DAY + timedelta(hours=hour)


def test_bias_counts_only_daylight_pairs_with_usable_values() -> None:
    obs = _series(
        DataKind.OBSERVATION, ASP, {_h(3): 800.0, _h(4): 900.0, _h(5): 700.0, _h(13): 0.0}
    )
    era5 = _series(
        DataKind.ANALYSIS,
        CELL,
        {_h(3): 850.0, _h(4): None, _h(5): 690.0, _h(13): 40.0, _h(20): 1.0},
        model_name="era5",
    )
    stats = bias_statistics(era5, obs)
    assert stats.n_common_steps == 4
    a = stats.agreement
    assert a is not None and a.n == 2  # 04 is missing, 13 UTC is night
    assert a.mean_bias == pytest.approx((50 - 10) / 2)
    assert a.mae == pytest.approx(30.0)
    assert a.rmse == pytest.approx(np.sqrt((50**2 + 10**2) / 2))
    assert a.mean_reference == pytest.approx(750.0)
    assert a.relative_bias == pytest.approx(20 / 750)
    assert (stats.first_utc, stats.last_utc) == (_h(3), _h(5))
    assert stats.candidate.startswith("test/era5")
    assert 20_000 < stats.separation_m < 30_000
    assert stats.provenance.kind is DataKind.DERIVED


def test_no_overlap_gives_no_agreement() -> None:
    obs = _series(DataKind.OBSERVATION, ASP, {_h(3): 800.0})
    other = _series(DataKind.ANALYSIS, CELL, {_h(4): 800.0})
    stats = bias_statistics(other, obs)
    assert stats.agreement is None and stats.n_common_steps == 0


def test_reference_must_be_an_observation() -> None:
    a = _series(DataKind.ANALYSIS, CELL, {_h(3): 800.0})
    with pytest.raises(ValueError, match="observation"):
        bias_statistics(a, a)


def test_series_must_be_hourly() -> None:
    obs = _series(DataKind.OBSERVATION, ASP, {_h(3): 800.0})
    coarse = _series(DataKind.ANALYSIS, CELL, {_h(3): 800.0}, temporal_resolution_s=10800.0)
    with pytest.raises(ValueError, match="hourly"):
        bias_statistics(coarse, obs)
