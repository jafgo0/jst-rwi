"""verify(): works on anything satisfying ForecastArchive and is explicit about gaps."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from rwi.contracts import ForecastArchive, ForecastObservationPair, WeatherSeries
from rwi.core.geo import GeoPoint
from rwi.core.timebase import TimeWindow
from rwi.verification import verify

T0 = datetime(2027, 10, 20, 0, 0, tzinfo=UTC)
NOW = datetime(2027, 10, 25, 0, 0, tzinfo=UTC)
POINT = GeoPoint(latitude_deg=-12.4, longitude_deg=130.9)
H = 3600.0


def make_pair(
    variable: str, k: int, *, bias: float = 1.0, lead_h: float = 8.0, provider: str = "open-meteo"
) -> ForecastObservationPair:
    valid = T0 + timedelta(hours=k)
    return ForecastObservationPair(
        variable=variable,
        valid_time_utc=valid,
        forecast_value=20.0 + bias,
        observed_value=20.0,
        lead_time_s=lead_h * H,
        provider=provider,
        model_name="ecmwf_ifs025",
        forecast_point=POINT,
        observation_point=POINT,
        separation_m=0.0,
        time_offset_s=0.0,
        forecast_recorded_at_utc=valid - timedelta(seconds=lead_h * H),
    )


class StubArchive:
    """Minimal ForecastArchive: pairs held in a dict, calls recorded."""

    def __init__(
        self, pairs: dict[str, list[ForecastObservationPair]], *, fail_on: str | None = None
    ) -> None:
        self._pairs = pairs
        self._fail_on = fail_on
        self.calls: list[dict[str, object]] = []

    def record_series(self, series: WeatherSeries) -> int:
        return 0

    def pairs(
        self,
        *,
        variable: str,
        period: TimeWindow | None = None,
        provider: str | None = None,
        model_name: str | None = None,
        max_separation_m: float = 50_000.0,
        max_time_offset_s: float = 1800.0,
    ) -> list[ForecastObservationPair]:
        self.calls.append({"variable": variable, "period": period, "provider": provider})
        if variable == self._fail_on:
            raise RuntimeError("database disk image is malformed")
        found = self._pairs.get(variable, [])
        return [p for p in found if provider is None or p.provider == provider]

    def coverage(self) -> dict[str, object]:
        return {}


def test_stub_satisfies_protocol() -> None:
    assert isinstance(StubArchive({}), ForecastArchive)


def test_report_scores_and_names_variable_without_pairs() -> None:
    archive = StubArchive({"temperature_2m": [make_pair("temperature_2m", k) for k in range(40)]})
    report = verify(archive, variables=["temperature_2m", "ghi"], now=NOW)
    assert report.pair_count == 40
    assert report.variables == ("temperature_2m", "ghi")
    assert [s.variable for s in report.scores] == ["temperature_2m"]
    assert report.scores[0].sample_count == 40
    assert report.scores[0].bias == 1.0
    assert report.has_evidence is True
    ghi_notes = [n for n in report.notes if n.startswith("ghi:")]
    assert len(ghi_notes) == 1 and "no forecast/observation pairs" in ghi_notes[0]
    assert report.computed_at_utc == NOW


def test_report_period_is_requested_window_or_span_of_valid_times() -> None:
    archive = StubArchive({"temperature_2m": [make_pair("temperature_2m", k) for k in range(10)]})
    report = verify(archive, variables=["temperature_2m"], now=NOW)
    assert report.period is not None
    assert report.period.start_utc == T0
    assert report.period.end_utc == T0 + timedelta(hours=9)
    window = TimeWindow(start_utc=T0 - timedelta(days=1), end_utc=T0 + timedelta(days=1))
    report = verify(archive, variables=["temperature_2m"], period=window, now=NOW)
    assert report.period == window
    assert archive.calls[-1]["period"] == window


def test_all_empty_report_says_so() -> None:
    report = verify(StubArchive({}), variables=["temperature_2m", "ghi"], now=NOW)
    assert report.scores == []
    assert report.pair_count == 0
    assert report.period is None
    assert report.has_evidence is False
    assert len(report.notes) == 2
    assert all("no forecast/observation pairs" in n for n in report.notes)


def test_thin_buckets_are_flagged() -> None:
    archive = StubArchive({"temperature_2m": [make_pair("temperature_2m", k) for k in range(5)]})
    report = verify(archive, variables=["temperature_2m"], now=NOW)
    assert report.has_evidence is False
    assert any("only 5 pair(s)" in n and "6-12h" in n for n in report.notes)
    assert any("not evidence for any model" in n for n in report.notes)


def test_pairs_outside_all_buckets_are_reported() -> None:
    pairs = [make_pair("temperature_2m", k) for k in range(3)] + [
        make_pair("temperature_2m", 50, lead_h=300.0)
    ]
    report = verify(StubArchive({"temperature_2m": pairs}), variables=["temperature_2m"], now=NOW)
    assert report.pair_count == 4
    assert report.scores[0].sample_count == 3
    assert any("outside all buckets" in n for n in report.notes)


def test_providers_filter_is_forwarded() -> None:
    archive = StubArchive(
        {
            "temperature_2m": [
                make_pair("temperature_2m", k, provider="open-meteo") for k in range(3)
            ]
            + [make_pair("temperature_2m", k + 10, provider="bom") for k in range(2)]
        }
    )
    report = verify(archive, variables=["temperature_2m"], providers=["bom"], now=NOW)
    assert report.pair_count == 2
    assert {c["provider"] for c in archive.calls} == {"bom"}
    empty = verify(archive, variables=["temperature_2m"], providers=["nobody"], now=NOW)
    assert any("providers nobody" in n for n in empty.notes)


def test_archive_failure_becomes_a_note_not_an_exception() -> None:
    archive = StubArchive(
        {"temperature_2m": [make_pair("temperature_2m", k) for k in range(3)]},
        fail_on="ghi",
    )
    report = verify(archive, variables=["ghi", "temperature_2m"], now=NOW)
    assert report.pair_count == 3
    assert any(n.startswith("ghi: archive error") and "RuntimeError" in n for n in report.notes)


def test_unknown_variable_is_noted() -> None:
    report = verify(StubArchive({}), variables=["bogus"], now=NOW)
    assert any("bogus" in n and "not a canonical variable" in n for n in report.notes)


def test_bad_arguments() -> None:
    with pytest.raises(ValueError):
        verify(StubArchive({}), variables=[])
    with pytest.raises(ValueError):
        verify(StubArchive({}), variables=["ghi"], providers=[])


def test_non_beating_persistence_is_stated_plainly() -> None:
    pairs = []
    for k in range(40):
        obs = 10.0 + 2.0 * (k % 2)
        valid = T0 + timedelta(hours=k)
        pairs.append(
            ForecastObservationPair(
                variable="temperature_2m",
                valid_time_utc=valid,
                forecast_value=obs + 4.0,
                observed_value=obs,
                lead_time_s=H,
                provider="p",
                model_name="m",
                forecast_point=POINT,
                observation_point=POINT,
                separation_m=0.0,
                time_offset_s=0.0,
                forecast_recorded_at_utc=valid - timedelta(hours=1),
            )
        )
    report = verify(StubArchive({"temperature_2m": pairs}), variables=["temperature_2m"], now=NOW)
    assert report.scores[0].skill_vs_persistence == -1.0
    assert any("does not beat persistence" in n for n in report.notes)


def test_verification_does_not_import_archive_package() -> None:
    code = (
        "import sys, rwi.verification; "
        "assert 'rwi.archive' not in sys.modules, 'rwi.archive imported'"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
