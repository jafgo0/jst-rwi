"""Model-vs-truth check for ECMWF reanalysis along the BWSC-2025 race route.

This is a one-off exploration tool, not a regression test.

Original intent was to measure *forecast skill* over race week 2025. That is
not possible retroactively: Open-Meteo's historical-forecast-api preserves
only ~90 days of past forecast runs, so for 2025-08-24 (14 months ago at the
time this script was written) it silently falls back on the ERA5 archive, and
the two endpoints become identical.

What this script does instead
-----------------------------
Pulls two independent gridded estimates of hourly GHI for race week 2025 at
six route points, and scores one against the other:

* ECMWF-IFS reanalysis via Open-Meteo's archive-api. This is what our live
  pipeline reaches for in ``fetch_analysis``.
* NASA POWER satellite-derived GHI (CERES SYN1deg). Independent of ECMWF.

The question answered: "Does our model's analysis for the race region match
what a satellite-derived product measured?" If yes, our live forecasts for
the same region start from a sound base. If not, we know the direction and
size of the baseline bias before we go anywhere near a forecast.

Why NASA POWER rather than BoM
------------------------------
BoM Climate Data Online does not expose a stable scriptable URL for hourly
GHI; the daily zip file links carry a session nonce. NASA POWER has a plain
HTTPS API, hourly resolution, and global coverage, with CERES as the source,
which is independent of ECMWF. BoM observatory 1-minute GHI remains the
best truth source and is the honest next step.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from rwi.contracts import ForecastObservationPair
from rwi.core.geo import GeoPoint
from rwi.verification.metrics import score_pairs

ROUTE_POINTS: list[tuple[str, GeoPoint]] = [
    ("Darwin",         GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0)),
    ("Katherine",      GeoPoint(latitude_deg=-14.4650, longitude_deg=132.2635, elevation_m=108.0)),
    ("Tennant Creek",  GeoPoint(latitude_deg=-19.6500, longitude_deg=134.1900, elevation_m=376.0)),
    ("Alice Springs",  GeoPoint(latitude_deg=-23.6980, longitude_deg=133.8807, elevation_m=545.0)),
    ("Coober Pedy",    GeoPoint(latitude_deg=-29.0135, longitude_deg=134.7544, elevation_m=217.0)),
    ("Adelaide",       GeoPoint(latitude_deg=-34.9285, longitude_deg=138.6007, elevation_m=50.0)),
]

RACE_START = datetime(2025, 8, 24, 0, 0, tzinfo=UTC)
RACE_END = datetime(2025, 8, 30, 23, 0, tzinfo=UTC)


def _fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read())


def _ecmwf_archive_ghi(point: GeoPoint) -> dict[datetime, float]:
    """Hourly GHI from Open-Meteo's ECMWF-IFS archive at the given point."""
    params = {
        "latitude": f"{point.latitude_deg:.4f}",
        "longitude": f"{point.longitude_deg:.4f}",
        "start_date": RACE_START.date().isoformat(),
        "end_date": RACE_END.date().isoformat(),
        "hourly": "shortwave_radiation",
        "models": "ecmwf_ifs025",
        "timezone": "UTC",
    }
    payload = _fetch(f"https://archive-api.open-meteo.com/v1/archive?{urllib.parse.urlencode(params)}")
    hourly = payload.get("hourly", {})
    out: dict[datetime, float] = {}
    for ts, val in zip(hourly.get("time", []), hourly.get("shortwave_radiation", []), strict=False):
        if val is None:
            continue
        out[datetime.fromisoformat(ts).replace(tzinfo=UTC)] = float(val)
    return out


def _nasa_power_ghi(point: GeoPoint) -> dict[datetime, float]:
    """Hourly GHI from NASA POWER (CERES SYN1deg, satellite-derived).

    POWER returns one value per hour, representative of the surface
    irradiance over that hour. Units are W/m^2 after we read the
    ``ALLSKY_SFC_SW_DWN`` field. The index key is ``YYYYMMDDHH`` in UTC
    with hour 0-23, which we map into datetimes aligned with ECMWF.
    """
    params = {
        "parameters": "ALLSKY_SFC_SW_DWN",
        "community": "RE",
        "longitude": f"{point.longitude_deg:.4f}",
        "latitude": f"{point.latitude_deg:.4f}",
        "start": RACE_START.date().strftime("%Y%m%d"),
        "end": RACE_END.date().strftime("%Y%m%d"),
        "format": "JSON",
        "time-standard": "UTC",
    }
    url = f"https://power.larc.nasa.gov/api/temporal/hourly/point?{urllib.parse.urlencode(params)}"
    payload = _fetch(url)
    rows = payload.get("properties", {}).get("parameter", {}).get("ALLSKY_SFC_SW_DWN", {})
    out: dict[datetime, float] = {}
    for stamp, val in rows.items():
        if val is None or float(val) < 0:  # POWER uses -999 for missing
            continue
        when = datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=UTC)
        out[when] = float(val)
    return out


def _build_pairs(
    *,
    ecmwf_series: dict[datetime, float],
    nasa_series: dict[datetime, float],
    point: GeoPoint,
) -> list[ForecastObservationPair]:
    """One pair per hour common to both series.

    We treat ECMWF reanalysis as the forecast and NASA POWER as the
    observation. The verification scores (bias, MAE, RMSE) are then
    directly interpretable: positive bias means ECMWF reads higher GHI
    than the satellite-derived product.
    """
    pairs: list[ForecastObservationPair] = []
    for hour, ecmwf_val in ecmwf_series.items():
        nasa_val = nasa_series.get(hour)
        if nasa_val is None:
            continue
        pairs.append(ForecastObservationPair(
            variable="ghi",
            valid_time_utc=hour,
            forecast_value=ecmwf_val,
            observed_value=nasa_val,
            lead_time_s=0.0,
            provider="open-meteo",
            model_name="ecmwf_ifs025",
            forecast_point=point,
            observation_point=point,
            separation_m=0.0,
            time_offset_s=0.0,
            # Reanalysis is recorded after its valid time but for the
            # verifier the constraint is "forecast not recorded after
            # valid time". Smallest honest value: the hour before.
            forecast_recorded_at_utc=hour - timedelta(hours=1),
        ))
    return pairs


def main() -> None:
    print(f"Period: {RACE_START.date()} to {RACE_END.date()} (BWSC 2025 week)")
    print("Forecast series: ECMWF-IFS reanalysis (Open-Meteo archive)")
    print("Observation series: NASA POWER (CERES SYN1deg satellite-derived)")
    print()

    per_point: list[tuple[str, list[ForecastObservationPair]]] = []
    all_pairs: list[ForecastObservationPair] = []

    for name, point in ROUTE_POINTS:
        ecmwf = _ecmwf_archive_ghi(point)
        nasa = _nasa_power_ghi(point)
        pairs = _build_pairs(ecmwf_series=ecmwf, nasa_series=nasa, point=point)
        per_point.append((name, pairs))
        all_pairs.extend(pairs)
        print(
            f"  {name:14s}  ECMWF hours={len(ecmwf):4d}  "
            f"NASA hours={len(nasa):4d}  paired={len(pairs):4d}"
        )

    def _row(label: str, pairs: list[ForecastObservationPair]) -> None:
        if not pairs:
            print(f"  {label:16s}  no pairs")
            return
        scores = score_pairs(
            pairs=pairs, variable="ghi", unit="W/m2",
            lead_time_buckets_s=((0.0, 3600.0),),
        )
        for score in scores:
            mae = score.mean_absolute_error or 0.0
            rmse = score.root_mean_square_error or 0.0
            bias = score.bias or 0.0
            mean_obs = score.mean_observed or 0.0
            mean_fc = score.mean_forecast or 0.0
            print(
                f"  {label:16s}  n={score.sample_count:4d}  "
                f"ECMWF_mean={mean_fc:6.1f}  NASA_mean={mean_obs:6.1f}  "
                f"bias={bias:>+7.2f}  MAE={mae:6.2f}  RMSE={rmse:6.2f}"
            )
            break

    print()
    print("=== PER-POINT SCORES (ECMWF reanalysis vs NASA POWER, W/m^2) ===")
    for name, pairs in per_point:
        _row(name, pairs)

    print()
    print("=== POOLED (all route points) ===")
    _row("all points", all_pairs)


if __name__ == "__main__":
    main()
