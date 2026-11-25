"""Calibrate the cold-front detector (#10, #14, step B) with ERA5 as the reference.

Two experiments, both run with the 24 h-detrended pressure trough
(``pressure_detrend_24h=True``) and with the raw one, so the effect of the fix
is measured rather than asserted:

1. **Diurnal phase-locking on ERA5.** Run the detector on ERA5 at the 11 named
   BWSC waypoints over 2025-09-25..2025-11-05. Then run a Rayleigh test on the
   local solar hour of every detection. Real fronts are not tied to local time.
2. **Archived forecasts vs ERA5.** Take every archived ``ecmwf_ifs025`` run at
   the 7 archive sites, detect fronts in it, and match them one-to-one against
   fronts the same rules find in ERA5, within +-6 h. Report POD, FAR, CSI and
   timing error per lead bucket.

   Each forecast run starts about 12 h after its init. The detector needs 24 h
   of history, so the hours before a run's first step are filled from earlier
   runs at the same site, using the latest earlier init. The live
   ``/api/meteorologist`` path gets the same kind of history, a short-lead
   forecast look-back. Only events inside the stretch of each run that can be
   scored are counted. ERA5 events are clipped to the same stretch.

ERA5 is a reanalysis, not an observation. This measures whether the forecast
reproduces the analysis. It does not show the rules find real fronts.

Usage:
    uv run python -I scripts/validation/synoptic_calibration.py [--archive PATH]

Writes nothing. Paste the printed tables into docs/validation/synoptic-calibration.md.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rwi.alerts.synoptic import (
    DEFAULT_THRESHOLDS,
    FrontSignature,
    SYNOPTIC_VARIABLES,
    RoutePointSeries,
    SynopticThresholds,
    detect_front_passages,
)
from rwi.alerts.synoptic_calibration import (
    EventMatch,
    event_scores,
    local_solar_hour,
    match_events,
    phase_locking,
)
from rwi.api.routers.meteorologist import _synoptic_points
from rwi.config import get_settings
from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, measured
from rwi.core.timebase import TimeWindow
from rwi.providers.openmeteo import OpenMeteoProvider
from rwi.route.bwsc import load_bwsc_route

ROOT = Path(__file__).resolve().parents[2]
PHASE_WINDOW = (datetime(2025, 9, 25, tzinfo=UTC), datetime(2025, 11, 5, tzinfo=UTC))
MODEL = "ecmwf_ifs025"
TOLERANCE = timedelta(hours=6)
LEAD_BUCKETS_H = ((0, 24), (24, 48), (48, 72))
VARS = tuple(SYNOPTIC_VARIABLES)

_RAW = dataclasses.replace(DEFAULT_THRESHOLDS, pressure_detrend_24h=False)


def _all(f: FrontSignature) -> bool:
    return True


def _wind(f: FrontSignature) -> bool:
    return "wind_shift" in f.criteria_met


def _sig(f: FrontSignature) -> bool:
    return f.strength == "signature"


# (thresholds, post-filter applied to forecast AND reference detections)
VARIANTS: dict[str, tuple[SynopticThresholds, Callable[[FrontSignature], bool]]] = {
    "raw": (_RAW, _all),
    "detrended": (DEFAULT_THRESHOLDS, _all),
    "detrended+wind_shift": (DEFAULT_THRESHOLDS, _wind),
    "detrended+signature": (DEFAULT_THRESHOLDS, _sig),
}


async def fetch_era5(points: list[GeoPoint], window: TimeWindow) -> list[WeatherSeries]:
    async with OpenMeteoProvider(get_settings()) as p:
        return list(
            await asyncio.gather(
                *(p.fetch_analysis(pt, window, SYNOPTIC_VARIABLES, models="era5") for pt in points)
            )
        )


def _last_valid(series: WeatherSeries) -> datetime:
    """Last hour where ERA5 has pressure (the archive lags real time by about 5 days)."""
    return max(s.valid_time_utc for s in series.steps if s.value("pressure_msl") is not None)


# ----------------------------------------------------------------------------- experiment 1


def phase_experiment() -> None:
    pts = _synoptic_points(load_bwsc_route())
    win = TimeWindow(start_utc=PHASE_WINDOW[0], end_utc=PHASE_WINDOW[1])
    era5 = asyncio.run(fetch_era5([p for _, _, p in pts], win))
    rps = [RoutePointSeries(n, d, p, s) for (n, d, p), s in zip(pts, era5, strict=True)]
    print(f"## 1. Diurnal phase-locking, ERA5, {win.start_utc:%Y-%m-%d}..{win.end_utc:%Y-%m-%d}\n")
    print("| variant | n | per point (N->S) | signature | R1 | p1 | R2 | mode h (12 h) | p2 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for name, (thr, keep) in VARIANTS.items():
        hours: list[float] = []
        per_pt: list[int] = []
        sig = 0
        for rp in rps:
            fs = [f for f in detect_front_passages(rp, thr, now=win.end_utc) if keep(f)]
            per_pt.append(len(fs))
            sig += sum(f.strength == "signature" for f in fs)
            hours += [local_solar_hour(f.passage_time_utc, rp.point.longitude_deg) for f in fs]
        p1, p2 = phase_locking(hours), phase_locking(hours, harmonic=2)
        print(
            f"| {name} | {p1.n} | {' '.join(map(str, per_pt))} | {sig} | "
            f"{p1.mean_resultant_length:.2f} | {p1.p_value:.1e} | "
            f"{p2.mean_resultant_length:.2f} | {p2.mean_local_hour:.1f} | {p2.p_value:.1e} |"
        )
    print()


# ----------------------------------------------------------------------------- experiment 2


@dataclasses.dataclass
class _Run:
    init: datetime
    point: GeoPoint
    values: dict[datetime, dict[str, float]]


def load_runs(db: Path) -> dict[tuple[float, float], list[_Run]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select s.id, s.model_init_time_utc, s.latitude_deg, s.longitude_deg, "
        "v.valid_time_utc, v.variable, v.value from series s join value v on v.series_id = s.id "
        f"where s.model_name = ? and v.variable in ({','.join('?' * len(VARS))}) "
        "and v.value is not null and s.model_init_time_utc is not null",
        (MODEL, *VARS),
    )
    runs: dict[int, _Run] = {}
    for sid, init, lat, lon, vt, var, val in rows:
        run = runs.get(sid)
        if run is None:
            run = runs[sid] = _Run(
                datetime.fromtimestamp(init, UTC), GeoPoint(latitude_deg=lat, longitude_deg=lon), {}
            )
        run.values.setdefault(datetime.fromtimestamp(vt, UTC), {})[var] = val
    con.close()
    by_site: dict[tuple[float, float], list[_Run]] = defaultdict(list)
    for run in runs.values():
        key = (run.point.latitude_deg, run.point.longitude_deg)
        by_site[key].append(run)
    for lst in by_site.values():
        lst.sort(key=lambda r: r.init)
    return by_site


def _to_series(point: GeoPoint, values: dict[datetime, dict[str, float]], init: datetime | None,
               kind: DataKind) -> WeatherSeries:
    steps = []
    for t in sorted(values):
        prov = Provenance(provider="open-meteo", dataset="archive", kind=kind,
                          retrieved_at_utc=t, valid_time_utc=t, point=point,
                          model_name=MODEL, model_init_time_utc=init)
        steps.append(WeatherStep(valid_time_utc=t, values={
            v: measured(v, x, prov) for v, x in values[t].items()}))
    return WeatherSeries(point=point, requested_point=point, provider="open-meteo",
                         dataset="archive", kind=kind, model_name=MODEL,
                         model_init_time_utc=init, retrieved_at_utc=init or datetime.now(UTC),
                         temporal_resolution_s=3600.0, steps=steps)


def _stitched(run: _Run, earlier: list[_Run]) -> dict[datetime, dict[str, float]]:
    """``run`` plus 27 h of history from the latest earlier runs covering each missing hour."""
    out = dict(run.values)
    first = min(run.values)
    t = first - timedelta(hours=27)
    while t < first:
        for prev in reversed(earlier):
            if t in prev.values:
                out.setdefault(t, prev.values[t])
                break
        t += timedelta(hours=1)
    return out


def forecast_experiment(db: Path) -> None:
    by_site = load_runs(db)
    sites = sorted(by_site, reverse=True)  # north to south
    t0 = min(r.init for lst in by_site.values() for r in lst) - timedelta(days=2)
    t1 = max(r.init for lst in by_site.values() for r in lst) + timedelta(days=3)
    era5 = asyncio.run(
        fetch_era5([by_site[s][0].point for s in sites], TimeWindow(start_utc=t0, end_utc=t1))
    )
    print(f"## 2. {MODEL} archived runs vs ERA5 (+-{TOLERANCE.total_seconds() / 3600:.0f} h)\n")
    for name, (thr, keep) in VARIANTS.items():
        buckets: dict[tuple[int, int], list[EventMatch]] = defaultdict(list)
        n_runs = 0
        era5_end = None
        ref_per_site: list[int] = []
        for site, ref_series in zip(sites, era5, strict=True):
            era5_end = _last_valid(ref_series)
            ref = [f for f in detect_front_passages(
                RoutePointSeries("ref", 0.0, ref_series.point, ref_series), thr, now=t1
            ) if keep(f)]
            runs = by_site[site]
            win_lo = min(min(r.values) for r in runs)
            ref_per_site.append(sum(win_lo <= f.passage_time_utc <= era5_end for f in ref))
            for i, run in enumerate(runs):
                vals = _stitched(run, runs[:i])
                first, last = min(run.values), max(run.values)
                if last > era5_end:
                    continue  # no reference yet for the end of this run
                n_runs += 1
                fc = [f for f in detect_front_passages(
                    RoutePointSeries("fc", 0.0, run.point,
                                     _to_series(run.point, vals, run.init, DataKind.FORECAST)),
                    thr, now=run.init,
                ) if keep(f)]
                lo, hi = first, last - timedelta(hours=thr.half_window_h)
                for b in LEAD_BUCKETS_H:
                    b_lo = max(lo, run.init + timedelta(hours=b[0]))
                    b_hi = min(hi, run.init + timedelta(hours=b[1]))
                    if b_lo >= b_hi:
                        continue
                    # widen reference by the tolerance so edge events can still match
                    f_t = [f.passage_time_utc for f in fc if b_lo <= f.passage_time_utc < b_hi]
                    r_t = [f.passage_time_utc for f in ref
                           if b_lo - TOLERANCE <= f.passage_time_utc < b_hi + TOLERANCE]
                    m = match_events(f_t, r_t, TOLERANCE)
                    # a reference event outside the bucket that found no partner is not a miss
                    m = EventMatch(m.hits, tuple(r for r in m.misses if b_lo <= r < b_hi),
                                   m.false_alarms)
                    buckets[b].append(m)
        print(f"### variant: {name} ({n_runs} runs, ERA5 to {era5_end:%Y-%m-%d %H} UTC)\n")
        print(f"Distinct ERA5 events per site (N->S): {' '.join(map(str, ref_per_site))}\n")
        print("| lead h | hits | misses | false alarms | POD | FAR | CSI | mean dt h | MAE dt h |")
        print("|---|---|---|---|---|---|---|---|---|")
        for b in LEAD_BUCKETS_H:
            s = event_scores(buckets[b])

            def f(x: float | None) -> str:
                return "n/a" if x is None else f"{x:.2f}"

            print(f"| {b[0]}-{b[1]} | {s.n_hits} | {s.n_misses} | {s.n_false_alarms} | "
                  f"{f(s.pod)} | {f(s.far)} | {f(s.csi)} | {f(s.mean_timing_error_h)} | "
                  f"{f(s.mean_abs_timing_error_h)} |")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--archive", type=Path, default=ROOT / "data/archive/rwi.sqlite3")
    ap.add_argument("--skip-phase", action="store_true")
    args = ap.parse_args()
    if not args.skip_phase:
        phase_experiment()
    forecast_experiment(args.archive)


if __name__ == "__main__":
    main()
