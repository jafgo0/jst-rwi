"""Calibrate the cold-front detector (#10, #14, step B) against station observations.

The step-B run with ERA5 as the reference (``synoptic_calibration.py``) showed
whether the forecast reproduces the analysis. This script puts observations
in place of ERA5:

1. **Observations alone.** Run the detector on hourly NOAA ISD data at 6 route
   stations (Darwin, Tennant Creek, Alice Springs, Woomera, Port Augusta,
   Adelaide) for each variant. Report event counts and phase locking, which is a
   Rayleigh test on local solar hour.
2. **ERA5 vs observations.** Fetch ERA5 at the station coordinates, run the same
   rules, and match events one-to-one within +-6 h. Report POD, FAR and CSI for
   the whole period and for the Sep-Nov race season.
3. **One-at-a-time threshold sensitivity.** Repeat steps 1-2 with one
   threshold moved at a time around the default.
4. **Archived forecasts vs archived BoM observations (Nov 2026).** Use the
   ``ecmwf_ifs025`` runs and stitching from ``synoptic_calibration.py``. The
   reference is the BoM 72 h station series at the nearest forecast site.

An event is scored only where the observation could evaluate the trough
criterion, that is where MSLP is present at every hour t-3..t+3 and at t-24 h.
Missing station data therefore does not count as a miss or a false alarm.

The reference is the same rules applied to observations. That is not an
independent front analysis, because no BoM chart archive has been accessed.
The question this answers: do the rules find in the real atmosphere what they
find in the model?

Data:
* ISD global-hourly CSV, one file per station and year, from
  ``https://noaa-global-hourly-pds.s3.amazonaws.com/<year>/<USAF><WBAN>.csv``
  (NOAA Open Data Dissemination). Put each year in its own directory and pass
  the directories with ``--isd``.
* BoM observations come from ``data/archive/rwi.sqlite3``, read-only. The
  BoM licence allows internal use only, so do not publish their values.

Usage:
    uv run python -I scripts/validation/synoptic_obs_calibration.py \\
        --isd DIR [DIR ...] [--archive PATH] [--skip-sweep]

Writes nothing. Paste the tables into docs/validation/synoptic-calibration.md.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import synoptic_calibration as sc  # noqa: E402

from rwi.alerts.synoptic import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    FrontSignature,
    RoutePointSeries,
    SynopticThresholds,
    detect_front_passages,
)
from rwi.alerts.synoptic_calibration import (  # noqa: E402
    EventMatch,
    event_scores,
    local_solar_hour,
    match_events,
    phase_locking,
)
from rwi.alerts.synoptic_obs import IsdReport, hourly_grid, parse_isd_row  # noqa: E402
from rwi.contracts import WeatherSeries, WeatherStep  # noqa: E402
from rwi.core.geo import GeoPoint  # noqa: E402
from rwi.core.provenance import DataKind, Provenance, measured  # noqa: E402
from rwi.core.timebase import TimeWindow  # noqa: E402

TOL = sc.TOLERANCE
SEASON_MONTHS = (9, 10, 11)
Grid = dict[datetime, dict[str, float]]


@dataclasses.dataclass
class Station:
    usaf: str
    name: str
    point: GeoPoint
    grid: Grid


def _f(x: float | None, fmt: str = ".2f") -> str:
    return "n/a" if x is None else format(x, fmt)


def load_isd(dirs: list[Path]) -> list[Station]:
    reps: dict[str, list[IsdReport]] = defaultdict(list)
    meta: dict[str, tuple[str, float, float]] = {}
    for d in dirs:
        for f in sorted(d.glob("*.csv")):
            with f.open(newline="") as fh:
                for row in csv.DictReader(fh):
                    sid = row["STATION"]
                    meta.setdefault(sid, (row["NAME"].split(",")[0].title(),
                                          float(row["LATITUDE"]), float(row["LONGITUDE"])))
                    r = parse_isd_row(row)
                    if r is not None:
                        reps[sid].append(r)
    out = [Station(sid[:5], meta[sid][0],
                   GeoPoint(latitude_deg=meta[sid][1], longitude_deg=meta[sid][2]),
                   hourly_grid(reps[sid])) for sid in reps]
    out.sort(key=lambda s: -s.point.latitude_deg)  # north to south
    return out


def evaluable(grid: Grid) -> Callable[[datetime], bool]:
    """True where the observation can evaluate the detrended trough at t."""
    p = {t for t, v in grid.items() if "pressure_msl" in v}

    def ok(t: datetime) -> bool:
        return (t - timedelta(hours=24)) in p and all(
            (t + timedelta(hours=k)) in p for k in range(-3, 4))
    return ok


# provider, dataset, model per data kind (labels only; the detector reads values)
_SRC = {DataKind.OBSERVATION: ("noaa-isd|bom", "station-hourly", None),
        DataKind.ANALYSIS: ("open-meteo", "archive", "era5"),
        DataKind.FORECAST: ("open-meteo", "archive", sc.MODEL)}


def _series(point: GeoPoint, grid: Grid, kind: DataKind) -> WeatherSeries:
    if kind is not DataKind.OBSERVATION:
        return sc._to_series(point, grid, None, kind)
    provider, dataset, model = _SRC[kind]
    steps = []
    for t in sorted(grid):
        prov = Provenance(provider=provider, dataset=dataset, kind=kind, retrieved_at_utc=t,
                          observation_time_utc=t, point=point, model_name=model)
        steps.append(WeatherStep(valid_time_utc=t, values={
            v: measured(v, x, prov) for v, x in grid[t].items()}))
    return WeatherSeries(point=point, requested_point=point, provider=provider, dataset=dataset,
                         kind=kind, retrieved_at_utc=max(grid), temporal_resolution_s=3600.0,
                         steps=steps)


def detect(point: GeoPoint, grid: Grid, thr: SynopticThresholds,
           keep: Callable[[FrontSignature], bool], kind: DataKind) -> list[datetime]:
    s = _series(point, grid, kind)
    end = max(grid) if grid else datetime.now(UTC)
    return [f.passage_time_utc for f in detect_front_passages(
        RoutePointSeries("x", 0.0, point, s), thr, now=end) if keep(f)]


def _grid_of(series: WeatherSeries) -> Grid:
    return {st.valid_time_utc: {k: q.value for k, q in st.values.items() if q.value is not None}
            for st in series.steps}


def _season(t: datetime) -> bool:
    return t.month in SEASON_MONTHS


# ----------------------------------------------------------------------------- 1 + 2


def obs_and_era5(stations: list[Station], era5: list[Grid],
                 variants: dict[str, tuple[SynopticThresholds, Callable[[FrontSignature], bool]]],
                 title: str) -> None:
    print(f"## {title}\n")
    print("| variant | obs n | obs per station (N->S) | obs/yr/station | R1 | p1 | R2 | p2 | "
          "ERA5 n | POD | FAR | CSI | season POD | season FAR | season CSI | MAE dt h |")
    print("|" + "---|" * 16)
    for name, (thr, keep) in variants.items():
        hours: list[float] = []
        per: list[int] = []
        n_era = 0
        allm: list[EventMatch] = []
        seam: list[EventMatch] = []
        years = 0.0
        for st, eg in zip(stations, era5, strict=True):
            ok = evaluable(st.grid)
            lo, hi = max(min(st.grid), min(eg)), min(max(st.grid), max(eg))
            years += (hi - lo).total_seconds() / (365.25 * 86400)
            o = [t for t in detect(st.point, st.grid, thr, keep, DataKind.OBSERVATION)
                 if lo <= t <= hi]
            e = [t for t in detect(st.point, eg, thr, keep, DataKind.ANALYSIS)
                 if lo <= t <= hi and ok(t)]
            per.append(len(o))
            n_era += len(e)
            hours += [local_solar_hour(t, st.point.longitude_deg) for t in o]
            allm.append(match_events(e, o, TOL))
            seam.append(match_events([t for t in e if _season(t)],
                                     [t for t in o if _season(t)], TOL))
        p1, p2 = phase_locking(hours), phase_locking(hours, harmonic=2)
        a, s = event_scores(allm), event_scores(seam)
        print(f"| {name} | {p1.n} | {' '.join(map(str, per))} | {p1.n / years:.1f} | "
              f"{_f(p1.mean_resultant_length)} | {_f(p1.p_value, '.1e')} | "
              f"{_f(p2.mean_resultant_length)} | {_f(p2.p_value, '.1e')} | {n_era} | "
              f"{_f(a.pod)} | {_f(a.far)} | {_f(a.csi)} | {_f(s.pod)} | {_f(s.far)} | "
              f"{_f(s.csi)} | {_f(a.mean_abs_timing_error_h)} |")
    print()


def sweep_variants() -> dict[str, tuple[SynopticThresholds, Callable[[FrontSignature], bool]]]:
    d = DEFAULT_THRESHOLDS
    out: dict[str, tuple[SynopticThresholds, Callable[[FrontSignature], bool]]] = {
        "default": (d, sc._all)}
    for field, vals in (("front_dp_rise_pa_3h", (50.0, 150.0, 200.0)),
                        ("front_wind_shift_deg", (45.0, 90.0)),
                        ("front_dt_drop_k_24h", (2.0, 4.0, 5.0)),
                        ("front_dtd_drop_k", (2.0, 4.0, 5.0)),
                        ("min_wind_m_s", (1.0, 3.0))):
        for v in vals:
            out[f"{field}={v:g}"] = (dataclasses.replace(d, **{field: v}), sc._all)  # type: ignore[arg-type]
    return out


# ----------------------------------------------------------------------------- 4


def bom_grids(db: Path) -> list[tuple[GeoPoint, Grid]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select s.id, s.latitude_deg, s.longitude_deg, v.valid_time_utc, v.variable, v.value "
        "from series s join value v on v.series_id = s.id where s.provider = 'bom' and "
        f"s.kind = 'observation' and v.variable in ({','.join('?' * len(sc.VARS))}) "
        "and v.value is not null", sc.VARS)
    by: dict[int, tuple[GeoPoint, dict[datetime, dict[str, float]]]] = {}
    for sid, lat, lon, vt, var, val in rows:
        _, raw = by.setdefault(sid, (GeoPoint(latitude_deg=lat, longitude_deg=lon), {}))
        raw.setdefault(datetime.fromtimestamp(vt, UTC), {})[var] = val
    con.close()
    return [(pt, hourly_grid(IsdReport(t, "bom", v) for t, v in raw.items()))
            for pt, raw in by.values()]


def forecast_vs_bom(db: Path) -> None:
    by_site = sc.load_runs(db)
    obs = bom_grids(db)
    print(f"## 4. {sc.MODEL} archived runs vs archived BoM observations (+-6 h)\n")
    for name, (thr, keep) in sc.VARIANTS.items():
        buckets: dict[tuple[int, int], list[EventMatch]] = defaultdict(list)
        n_runs = 0
        per_site: list[str] = []
        for opt, og in sorted(obs, key=lambda x: -x[0].latitude_deg):
            site = min(by_site, key=lambda k: (k[0] - opt.latitude_deg) ** 2
                       + (k[1] - opt.longitude_deg) ** 2)
            ok = evaluable(og)
            ref = detect(opt, og, thr, keep, DataKind.OBSERVATION)
            per_site.append(str(len(ref)))
            runs = by_site[site]
            o_end = max(og)
            for i, run in enumerate(runs):
                first, last = min(run.values), max(run.values)
                if last > o_end:
                    continue
                n_runs += 1
                vals = sc._stitched(run, runs[:i])
                fc = detect(run.point, vals, thr, keep, DataKind.FORECAST)
                lo, hi = first, last - timedelta(hours=thr.half_window_h)
                for b in sc.LEAD_BUCKETS_H:
                    b_lo = max(lo, run.init + timedelta(hours=b[0]))
                    b_hi = min(hi, run.init + timedelta(hours=b[1]))
                    if b_lo >= b_hi:
                        continue
                    f_t = [t for t in fc if b_lo <= t < b_hi and ok(t)]
                    r_t = [t for t in ref if b_lo - TOL <= t < b_hi + TOL]
                    m = match_events(f_t, r_t, TOL)
                    buckets[b].append(EventMatch(
                        m.hits, tuple(r for r in m.misses if b_lo <= r < b_hi), m.false_alarms))
        print(f"### variant: {name} ({n_runs} run-site pairs)\n")
        print(f"Obs events per BoM station (N->S): {' '.join(per_site)}\n")
        print("| lead h | hits | misses | false alarms | POD | FAR | CSI | mean dt h | MAE dt h |")
        print("|---|---|---|---|---|---|---|---|---|")
        for b in sc.LEAD_BUCKETS_H:
            s = event_scores(buckets[b])
            print(f"| {b[0]}-{b[1]} | {s.n_hits} | {s.n_misses} | {s.n_false_alarms} | "
                  f"{_f(s.pod)} | {_f(s.far)} | {_f(s.csi)} | {_f(s.mean_timing_error_h)} | "
                  f"{_f(s.mean_abs_timing_error_h)} |")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--isd", type=Path, nargs="+", required=True)
    ap.add_argument("--archive", type=Path, default=ROOT / "data/archive/rwi.sqlite3")
    ap.add_argument("--skip-sweep", action="store_true")
    args = ap.parse_args()
    stations = load_isd(args.isd)
    t0 = min(min(s.grid) for s in stations)
    t1 = max(max(s.grid) for s in stations)
    print(f"Stations (N->S): {', '.join(f'{s.name} {s.usaf}' for s in stations)}; "
          f"ISD {t0:%Y-%m-%d}..{t1:%Y-%m-%d}\n")
    era5 = [_grid_of(s) for s in asyncio.run(sc.fetch_era5(
        [s.point for s in stations], TimeWindow(start_utc=t0, end_utc=t1)))]
    obs_and_era5(stations, era5, sc.VARIANTS, "1-2. Observations and ERA5 vs observations")
    if not args.skip_sweep:
        obs_and_era5(stations, era5, sweep_variants(), "3. One-at-a-time threshold sensitivity")
    forecast_vs_bom(args.archive)


if __name__ == "__main__":
    main()
