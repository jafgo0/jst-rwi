"""Every quality check fires on a crafted series and stays silent on a clean one."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx

from rwi.config import Settings
from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, QualityFlag, measured
from rwi.core.timebase import TimeWindow
from rwi.providers.bom import BomProvider
from rwi.providers.openmeteo import OpenMeteoProvider
from rwi.quality import QualityReport, analyse_series, check_series, check_value, summarise

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
T0 = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=1)
POINT = GeoPoint(latitude_deg=-12.5, longitude_deg=130.75, elevation_m=32.0)
OK, MISSING, OOR = QualityFlag.OK, QualityFlag.MISSING, QualityFlag.OUT_OF_RANGE
RATE, SUSPECT, STALE = QualityFlag.RATE_IMPLAUSIBLE, QualityFlag.SENSOR_SUSPECT, QualityFlag.STALE


def build(
    rows: list[dict[str, float | None]],
    *,
    kind: DataKind = DataKind.FORECAST,
    step_s: float = 3600.0,
    times: list[datetime] | None = None,
    retrieved: datetime | None = None,
) -> WeatherSeries:
    retrieved = retrieved or NOW
    steps = []
    for i, row in enumerate(rows):
        t = times[i] if times else T0 + timedelta(seconds=step_s * i)
        extra: dict[str, Any] = (
            {"observation_time_utc": t} if kind is DataKind.OBSERVATION else {"valid_time_utc": t}
        )
        prov = Provenance(provider="t", dataset="d", kind=kind, retrieved_at_utc=retrieved, **extra)
        steps.append(
            WeatherStep(valid_time_utc=t, values={k: measured(k, v, prov) for k, v in row.items()})
        )
    return WeatherSeries(
        point=POINT, provider="t", dataset="d", kind=kind, retrieved_at_utc=retrieved, steps=steps
    )


def flags(series: WeatherSeries, variable: str) -> list[QualityFlag]:
    return [s.values[variable].quality for s in series.steps]


def clean_rows(n: int = 8) -> list[dict[str, float | None]]:
    """A physically sensible, varying daytime sequence."""
    return [
        {
            "temperature_2m": 25.0 + 0.7 * i,
            "dew_point_2m": 18.0 + 0.3 * i,
            "relative_humidity_2m": 60.0 - 1.5 * i,
            "pressure_msl": 101300.0 - 40.0 * i,
            "wind_speed_10m": 3.0 + 0.2 * i,
            "wind_gusts_10m": 6.0 + 0.3 * i,
            "wind_direction_10m": 90.0 + 5.0 * i,
            "ghi": 600.0 + 20.0 * i,
            "dni": 700.0 + 10.0 * i,
            "dhi": 100.0 + 5.0 * i,
            "is_day": 1.0,
            "cloud_cover": 20.0 + 3.0 * i,
            "precipitation": 0.0,
        }
        for i in range(n)
    ]


# --------------------------------------------------------------------------- clean data


def test_clean_series_gets_no_flags_and_no_issues():
    s = build(clean_rows())
    checked, report = analyse_series(s, now=NOW)
    assert all(m.quality is OK for st in checked.steps for m in st.values.values())
    assert report.issues == [] and report.is_clean
    assert report.usable_fraction == 1.0 and report.usable_count == report.value_count


def test_legitimately_constant_variables_are_not_called_frozen():
    rows = [
        {
            "ghi": 0.0,
            "dni": 0.0,
            "dhi": 0.0,
            "is_day": 0.0,
            "wind_speed_10m": 0.0,
            "precipitation": 0.0,
            "cloud_cover": 100.0,
            "relative_humidity_2m": 100.0,
            "temperature_2m": 22.0 + 0.2 * i,
        }
        for i in range(10)
    ]
    checked = check_series(build(rows), now=NOW)
    assert all(m.quality is OK for st in checked.steps for m in st.values.values())


# --------------------------------------------------------------------------- missing / range


def test_missing_value_is_flagged_missing_and_kept_as_none():
    rows = clean_rows(3)
    rows[1]["temperature_2m"] = None
    checked = check_series(build(rows), now=NOW)
    assert flags(checked, "temperature_2m") == [OK, MISSING, OK]
    assert checked.steps[1].values["temperature_2m"].value is None  # not replaced by 0


def test_out_of_range_is_flagged_and_value_left_untouched():
    rows = clean_rows(3)
    rows[2]["temperature_2m"] = 75.0
    rows[0]["relative_humidity_2m"] = 130.0
    checked = check_series(build(rows), now=NOW)
    assert flags(checked, "temperature_2m") == [OK, OK, OOR]
    assert checked.steps[2].values["temperature_2m"].value == 75.0
    assert checked.steps[0].values["relative_humidity_2m"].quality is OOR
    assert checked.steps[2].value("temperature_2m") is None  # unusable downstream


def test_range_boundaries_are_inclusive():
    for edge in (0.0, 100.0):
        one = build([{"relative_humidity_2m": edge}])
        assert flags(check_series(one, now=NOW), "relative_humidity_2m") == [OK]
    just_over = build([{"relative_humidity_2m": 100.01}])
    assert flags(check_series(just_over, now=NOW), "relative_humidity_2m") == [OOR]


# --------------------------------------------------------------------------- rate of change


def test_implausible_rate_of_change_flags_the_later_value():
    rows = [{"temperature_2m": 20.0}, {"temperature_2m": 45.0}, {"temperature_2m": 45.5}]
    checked, report = analyse_series(build(rows), now=NOW)
    assert flags(checked, "temperature_2m") == [OK, RATE, OK]
    issue = next(i for i in report.issues if i.check == "rate")
    assert issue.variable == "temperature_2m" and "25" in issue.detail


def test_plausible_change_and_sub_hourly_scaling():
    ok = build([{"temperature_2m": 20.0}, {"temperature_2m": 28.0}])  # 8 K/h < 15
    assert flags(check_series(ok, now=NOW), "temperature_2m") == [OK, OK]
    # 10 K in 30 min = 20 K/h > 15 K/h -> flagged on a half-hourly observation series
    fast = build([{"temperature_2m": 20.0}, {"temperature_2m": 30.0}], step_s=1800)
    assert flags(check_series(fast, now=NOW), "temperature_2m") == [OK, RATE]


def test_rate_check_compares_against_previous_plausible_value_across_gaps():
    rows = [{"temperature_2m": 20.0}, {"temperature_2m": None}, {"temperature_2m": 21.0}]
    assert flags(check_series(build(rows), now=NOW), "temperature_2m") == [OK, MISSING, OK]


def test_cloud_cover_full_swing_is_not_a_rate_violation():
    rows = [{"cloud_cover": 0.0}, {"cloud_cover": 100.0}]
    assert flags(check_series(build(rows), now=NOW), "cloud_cover") == [OK, OK]


# --------------------------------------------------------------------------- consistency


def test_dew_point_above_temperature_flags_both():
    rows = [
        {"temperature_2m": 20.0, "dew_point_2m": 22.0},
        {"temperature_2m": 20.0, "dew_point_2m": 20.0},
    ]
    checked = check_series(build(rows), now=NOW)
    assert flags(checked, "dew_point_2m") == [SUSPECT, OK]
    assert flags(checked, "temperature_2m") == [SUSPECT, OK]
    assert checked.steps[0].values["dew_point_2m"].value == 22.0  # flagged, not corrected


def test_dew_point_rounding_tolerance():
    rows = [{"temperature_2m": 20.0, "dew_point_2m": 20.05}]
    assert flags(check_series(build(rows), now=NOW), "dew_point_2m") == [OK]


def test_gust_below_mean_wind_is_flagged():
    rows = [
        {"wind_speed_10m": 8.0, "wind_gusts_10m": 5.0},
        {"wind_speed_10m": 8.0, "wind_gusts_10m": 8.0},
    ]
    checked = check_series(build(rows), now=NOW)
    assert flags(checked, "wind_gusts_10m") == [SUSPECT, OK]
    assert flags(checked, "wind_speed_10m") == [SUSPECT, OK]


def test_dhi_above_ghi_is_flagged_but_equal_is_fine():
    rows = [{"ghi": 400.0, "dhi": 450.0}, {"ghi": 400.0, "dhi": 400.0}, {"ghi": 0.0, "dhi": 0.0}]
    checked = check_series(build(rows), now=NOW)
    assert flags(checked, "dhi") == [SUSPECT, OK, OK] and flags(checked, "ghi") == [SUSPECT, OK, OK]


def test_ghi_positive_while_provider_says_night_is_flagged():
    rows = [
        {"ghi": 0.0, "is_day": 0.0},
        {"ghi": 300.0, "is_day": 0.0},
        {"ghi": 300.0, "is_day": 0.0},
    ]
    checked, report = analyse_series(build(rows), now=NOW)
    assert flags(checked, "ghi") == [OK, SUSPECT, SUSPECT]
    assert any(i.check == "ghi_zero_at_night" for i in report.issues)
    assert checked.steps[1].values["ghi"].value == 300.0  # not zeroed


def test_dusk_hour_with_preceding_hour_mean_is_not_flagged():
    """Open-Meteo GHI is the PRECEDING-hour mean: the first is_day=0 hour after sunset
    legitimately has GHI > 0 (real archive: 109 -> 2 W/m2)."""
    rows = [
        {"ghi": 109.0, "is_day": 1.0},
        {"ghi": 40.0, "is_day": 0.0},
        {"ghi": 0.0, "is_day": 0.0},
    ]
    assert flags(check_series(build(rows), now=NOW), "ghi") == [OK, OK, OK]
    tiny = [{"ghi": 2.0, "is_day": 0.0}, {"ghi": 2.0, "is_day": 0.0}]
    assert flags(check_series(build(tiny), now=NOW), "ghi") == [
        OK,
        OK,
    ]  # within the 5 W/m2 allowance


# --------------------------------------------------------------------------- timestamps


def test_duplicate_timestamps_are_flagged_everywhere_they_occur():
    times = [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=1), T0 + timedelta(hours=2)]
    s = build([{"temperature_2m": 20.0 + i} for i in range(4)], times=times)
    checked, report = analyse_series(s, now=NOW)
    assert flags(checked, "temperature_2m") == [OK, SUSPECT, SUSPECT, OK]
    assert any(i.check == "duplicate_timestamp" and i.variable is None for i in report.issues)
    assert len(checked.steps) == 4  # nothing deleted
    assert not report.is_clean


def test_non_monotonic_timestamps_are_flagged():
    good = build([{"temperature_2m": 20.0 + i} for i in range(3)])
    swapped = good.model_copy(update={"steps": [good.steps[0], good.steps[2], good.steps[1]]})
    checked, report = analyse_series(swapped, now=NOW)
    assert any(i.check == "non_monotonic_timestamp" for i in report.issues)
    assert flags(checked, "temperature_2m")[2] is SUSPECT
    assert [s.valid_time_utc for s in checked.steps] == [s.valid_time_utc for s in swapped.steps]


# --------------------------------------------------------------------------- frozen sensor


def test_frozen_sensor_is_flagged_only_at_six_or_more_identical_values():
    frozen = build([{"temperature_2m": 21.3} for _ in range(6)])
    checked, report = analyse_series(frozen, now=NOW)
    assert flags(checked, "temperature_2m") == [SUSPECT] * 6
    assert any(i.check == "frozen" for i in report.issues)
    short = build([{"temperature_2m": 21.3} for _ in range(5)])
    assert flags(check_series(short, now=NOW), "temperature_2m") == [OK] * 5


def test_frozen_pressure_and_wind_direction_are_flagged():
    rows = [{"pressure_msl": 101325.0, "wind_direction_10m": 180.0} for _ in range(7)]
    checked = check_series(build(rows), now=NOW)
    assert flags(checked, "pressure_msl") == [SUSPECT] * 7
    assert flags(checked, "wind_direction_10m") == [SUSPECT] * 7


# --------------------------------------------------------------------------- staleness


def test_stale_observations_are_flagged_stale_but_kept():
    obs_t = [NOW - timedelta(hours=3) + timedelta(minutes=30 * i) for i in range(3)]
    s = build(
        [{"temperature_2m": 25.0 + i} for i in range(3)],
        kind=DataKind.OBSERVATION,
        times=obs_t,
        retrieved=NOW - timedelta(minutes=1),
    )
    checked, report = analyse_series(s, now=NOW, max_age_s=1800.0)
    assert flags(checked, "temperature_2m") == [STALE] * 3
    assert [m.value for m in (st.values["temperature_2m"] for st in checked.steps)] == [
        25.0,
        26.0,
        27.0,
    ]
    assert any(i.check == "stale" for i in report.issues)


def test_fresh_observation_history_is_not_stale_just_because_old_steps_are_old():
    obs_t = [NOW - timedelta(minutes=10) - timedelta(hours=h) for h in (3, 2, 1, 0)]
    s = build(
        [{"temperature_2m": 25.0 + 0.1 * i} for i in range(4)],
        kind=DataKind.OBSERVATION,
        times=obs_t,
    )
    assert flags(check_series(s, now=NOW, max_age_s=1800.0), "temperature_2m") == [OK] * 4


def test_old_forecast_retrieval_is_stale_but_future_valid_time_is_not_an_age():
    old = build(clean_rows(3), retrieved=NOW - timedelta(hours=5))
    assert flags(check_series(old, now=NOW, max_age_s=3 * 3600), "temperature_2m") == [STALE] * 3
    fresh = build(clean_rows(3), retrieved=NOW - timedelta(minutes=5))
    assert flags(check_series(fresh, now=NOW, max_age_s=3 * 3600), "temperature_2m") == [OK] * 3


def test_default_age_limits_come_from_settings():
    # forecast default is max_forecast_age_s = 10800 s
    old = build(clean_rows(2), retrieved=NOW - timedelta(hours=5))
    assert flags(check_series(old, now=NOW), "temperature_2m") == [STALE, STALE]
    obs = build(
        [{"temperature_2m": 25.0}], kind=DataKind.OBSERVATION, times=[NOW - timedelta(hours=1)]
    )  # default 1800 s
    assert flags(check_series(obs, now=NOW), "temperature_2m") == [STALE]


def test_staleness_never_masks_a_more_severe_flag_and_recovers_on_refresh():
    rows = clean_rows(2)
    rows[1]["temperature_2m"] = 99.0
    s = build(rows, retrieved=NOW - timedelta(hours=5))
    checked = check_series(s, now=NOW, max_age_s=3600)
    assert flags(checked, "temperature_2m") == [STALE, OOR]
    later_but_fresh = check_series(checked, now=NOW - timedelta(hours=4), max_age_s=3600)
    assert flags(later_but_fresh, "temperature_2m") == [OK, OOR]  # STALE is re-evaluated


# --------------------------------------------------------------------------- contract behaviour


def test_check_series_never_deletes_mutates_or_reorders():
    rows = clean_rows(5)
    rows[2]["temperature_2m"] = None
    rows[3]["pressure_msl"] = 5.0
    s = build(rows)
    before = s.model_dump()
    checked = check_series(s, now=NOW)
    assert s.model_dump() == before  # input untouched
    assert [st.valid_time_utc for st in checked.steps] == [st.valid_time_utc for st in s.steps]
    for a, b in zip(s.steps, checked.steps, strict=True):
        assert {k: m.value for k, m in a.values.items()} == {
            k: m.value for k, m in b.values.items()
        }
    assert checked.provider == s.provider and checked.retrieved_at_utc == s.retrieved_at_utc


def test_check_series_is_idempotent():
    rows = clean_rows(6)
    rows[1]["temperature_2m"] = 80.0
    rows[4]["dew_point_2m"] = 40.0
    once = check_series(build(rows), now=NOW)
    twice = check_series(once, now=NOW)
    assert once.model_dump() == twice.model_dump()


def test_most_severe_flag_wins():
    rows = [
        {"temperature_2m": 20.0, "dew_point_2m": 21.0},
        {"temperature_2m": 99.0, "dew_point_2m": 98.0},
    ]
    checked = check_series(build(rows), now=NOW)
    assert (
        checked.steps[1].values["temperature_2m"].quality is OOR
    )  # not downgraded to RATE/SUSPECT


# --------------------------------------------------------------------------- check_value


def _m(variable: str, value: float | None, t: datetime = T0) -> Any:
    prov = Provenance(
        provider="t", dataset="d", kind=DataKind.FORECAST, retrieved_at_utc=NOW, valid_time_utc=t
    )
    return measured(variable, value, prov)


def test_check_value_missing_range_rate_and_clean():
    assert check_value(_m("temperature_2m", None)).quality is MISSING
    assert check_value(_m("temperature_2m", -120.0)).quality is OOR
    assert check_value(_m("temperature_2m", 25.0)).quality is OK
    prev = _m("temperature_2m", 20.0, T0)
    assert (
        check_value(_m("temperature_2m", 45.0, T0 + timedelta(hours=1)), previous=prev).quality
        is RATE
    )
    assert (
        check_value(_m("temperature_2m", 22.0, T0 + timedelta(hours=1)), previous=prev).quality
        is OK
    )
    original = _m("temperature_2m", 99.0)
    check_value(original)
    assert original.quality is OK  # input not mutated (frozen model, new copy returned)


def test_check_value_ignores_previous_of_another_variable_and_optional_staleness():
    prev = _m("pressure_msl", 101300.0, T0)
    cur = _m("temperature_2m", 45.0, T0 + timedelta(hours=1))
    assert check_value(cur, previous=prev).quality is OK
    assert check_value(cur, now=NOW + timedelta(hours=10), max_age_s=3600).quality is STALE


# --------------------------------------------------------------------------- summarise


def test_summarise_counts_per_flag_and_per_variable():
    rows = clean_rows(4)
    rows[0]["temperature_2m"] = None
    rows[1]["temperature_2m"] = 99.0
    rows[2]["pressure_msl"] = 5.0
    checked = check_series(build(rows), now=NOW)
    report = summarise(checked)
    assert isinstance(report, QualityReport)
    assert report.step_count == 4 and report.value_count == 4 * len(rows[0])
    assert report.count(MISSING) == 1 and report.count(OOR) == 2
    assert report.by_variable["temperature_2m"] == {"missing": 1, "out_of_range": 1, "ok": 2}
    assert sum(report.flag_counts.values()) == report.value_count
    assert set(report.flag_counts) == {f.value for f in QualityFlag}
    assert 0.0 < report.usable_fraction < 1.0
    assert not report.is_clean
    assert report.provider == "t" and report.kind is DataKind.FORECAST


# --------------------------------------------------------------------------- real captured data


@respx.mock
async def test_real_openmeteo_fixtures_pass_quality_control_cleanly(tmp_path):
    settings = Settings(_env_file=None, cache_dir=tmp_path, http_retries=0)
    clock = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    p = OpenMeteoProvider(settings, clock=lambda: clock)
    respx.get("https://api.open-meteo.com/v1/forecast").respond(
        200, json=json.loads((FIXTURES / "openmeteo_forecast_darwin_ifs025.json").read_text())
    )
    respx.get("https://archive-api.open-meteo.com/v1/archive").respond(
        200, json=json.loads((FIXTURES / "openmeteo_archive_darwin_ifs.json").read_text())
    )
    point = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456)
    variables = [
        "temperature_2m",
        "dew_point_2m",
        "pressure_msl",
        "wind_speed_10m",
        "wind_gusts_10m",
        "ghi",
        "dni",
        "dhi",
        "is_day",
    ]
    fc = await p.fetch_forecast(
        point,
        TimeWindow(
            start_utc=datetime(2026, 11, 2, 1, tzinfo=UTC),
            end_utc=datetime(2026, 11, 2, 5, tzinfo=UTC),
        ),
        [
            "temperature_2m",
            "dew_point_2m",
            "pressure_msl",
            "wind_speed_10m",
            "wind_gusts_10m",
            "ghi",
            "dni",
            "dhi",
            "is_day",
        ],
    )
    ar = await p.fetch_analysis(
        point,
        TimeWindow(
            start_utc=datetime(2026, 10, 20, 0, tzinfo=UTC),
            end_utc=datetime(2026, 10, 20, 23, tzinfo=UTC),
        ),
        variables,
    )
    _, report = analyse_series(fc, now=clock)
    assert report.issues == [] and report.is_clean and report.usable_fraction == 1.0
    # The archive day includes the sunset hour (GHI 2 W/m2 with is_day 0: must NOT fire) and
    # one hour where the model's gust diagnostic (18.7 km/h) is below its mean wind (20.4 km/h).
    # The latter is genuine model-output inconsistency and the check rightly reports it.
    _, report = analyse_series(ar, now=clock)
    assert len(ar.steps) == 24
    assert {i.check for i in report.issues} == {"gust_ge_mean_wind"}
    assert {i.variable for i in report.issues} == {"wind_gusts_10m", "wind_speed_10m"}
    assert report.count(SUSPECT) == 2 and report.usable_count == report.value_count - 2


@respx.mock
async def test_real_bom_fixture_passes_quality_control_cleanly(tmp_path):
    bom = json.loads((FIXTURES / "bom_idd60910_94120_darwin_trimmed.json").read_text())
    respx.get("http://www.bom.gov.au/fwo/IDD60910/IDD60910.94120.json").respond(
        301, headers={"Location": "https://www.bom.gov.au/fwo/IDD60910/IDD60910.94120.json"}
    )
    respx.get("https://www.bom.gov.au/fwo/IDD60910/IDD60910.94120.json").respond(200, json=bom)
    now = datetime(2026, 11, 1, 22, 10, tzinfo=UTC)
    p = BomProvider(Settings(_env_file=None, cache_dir=tmp_path, http_retries=0), clock=lambda: now)
    s = await p.fetch_observations(
        GeoPoint(latitude_deg=-12.47, longitude_deg=130.85),
        TimeWindow(start_utc=datetime(2026, 11, 1, 19, tzinfo=UTC), end_utc=now),
        [
            "temperature_2m",
            "dew_point_2m",
            "pressure_msl",
            "wind_speed_10m",
            "wind_gusts_10m",
            "relative_humidity_2m",
        ],
    )
    _, report = analyse_series(s, now=now)  # newest obs is 10 min old < 1800 s default
    assert report.issues == [], [i.detail for i in report.issues]
    # the same series examined two hours later is entirely STALE, and still complete
    later = check_series(s, now=now + timedelta(hours=2))
    assert all(m.quality is STALE for st in later.steps for m in st.values.values())
    assert len(later.steps) == len(s.steps)


def test_summarise_on_empty_series_is_safe():
    empty = WeatherSeries(
        point=POINT,
        provider="t",
        dataset="d",
        kind=DataKind.FORECAST,
        retrieved_at_utc=NOW,
        steps=[],
    )
    r = summarise(empty)
    assert r.step_count == 0 and r.value_count == 0 and r.usable_fraction == 0.0
    assert check_series(empty, now=NOW).steps == []


@pytest.mark.parametrize("kind", [DataKind.FORECAST, DataKind.OBSERVATION, DataKind.ANALYSIS])
def test_kind_is_preserved(kind):
    t = [T0]
    s = build([{"temperature_2m": 20.0}], kind=kind, times=t)
    assert check_series(s, now=NOW, max_age_s=10**9).kind is kind
