"""The pair query is an indexed range scan, and stays fast at modest scale."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_archive_support import T0, forecast_series, new_archive, observation_series

from rwi.core.timebase import TimeWindow

VAR = "wind_speed_10m"


def test_pair_query_plan_uses_the_variable_time_index(tmp_path: Path) -> None:
    with new_archive(tmp_path) as archive:
        archive.record_series(forecast_series())
        archive.record_series(observation_series())
        window = TimeWindow(start_utc=T0 - timedelta(days=1), end_utc=T0 + timedelta(days=1))
        plan = archive.explain_pairs(variable=VAR, period=window)

    text = "\n".join(plan)
    # Expected index: idx_value_variable_time (variable, valid_time_utc, series_id),
    # searched with a variable equality and a valid-time range, for BOTH the
    # forecast query and the observation query. The series side is reached by its
    # INTEGER PRIMARY KEY. INDEXED BY in the SQL makes a missing index a hard error.
    assert text.count("idx_value_variable_time") >= 2, text
    assert "variable=? AND valid_time_utc>? AND valid_time_utc<?" in text
    assert not [line for line in plan if line.startswith("SCAN")], text  # no full table scan


def test_scale_50k_rows_pair_query_within_budget(tmp_path: Path) -> None:
    """50,000 forecast values (250 model runs x 200 hours) plus ~450 hourly observations.

    Budget: the whole-archive pair query, including building 50,000 validated
    pydantic pairs, must finish in under 10 s; a 10-hour period query in under
    2 s. On the development machine they take roughly 1-2 s and a few ms. The
    bound is deliberately loose: it exists to catch an O(n^2) or full-scan
    regression, not to benchmark, so a loaded CI box does not flake. Building the
    fixture is not timed.
    """
    base = datetime(2027, 3, 1, tzinfo=UTC)
    n_runs, n_hours = 250, 200
    last_valid = base + timedelta(hours=n_runs + n_hours + 2)
    n_obs = int((last_valid - base).total_seconds() // 3600) + 1
    archive = new_archive(tmp_path, "scale.sqlite3")
    try:
        archive.record_series(
            observation_series(
                times=[base + timedelta(hours=h) for h in range(n_obs)],
                value=[float(h % 20) for h in range(n_obs)],
            )
        )
        for run in range(n_runs):
            init = base + timedelta(hours=run)
            archive.record_series(
                forecast_series(
                    valid_times=[init + timedelta(hours=1 + h) for h in range(n_hours)],
                    value=[float(h % 20) + 0.5 for h in range(n_hours)],
                    init=init,
                    retrieved=init + timedelta(minutes=30),
                )
            )
        cov = archive.coverage(include_pair_counts=False)
        assert cov["value_count"] == n_runs * n_hours + n_obs

        start = time.perf_counter()
        pairs = archive.pairs(variable=VAR)
        elapsed = time.perf_counter() - start
        assert len(pairs) == n_runs * n_hours
        assert elapsed < 10.0, f"pair query took {elapsed:.2f}s"

        window = TimeWindow(
            start_utc=base + timedelta(hours=100), end_utc=base + timedelta(hours=110)
        )
        start = time.perf_counter()
        small = archive.pairs(variable=VAR, period=window)
        assert time.perf_counter() - start < 2.0
        assert 0 < len(small) < len(pairs)
        assert all(window.start_utc <= p.valid_time_utc < window.end_utc for p in small)
    finally:
        archive.close()
