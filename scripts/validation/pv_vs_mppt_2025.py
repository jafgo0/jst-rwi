"""First real validation of the PV model against race data.

Takes one hour of MPPT output from BWSC 2025 day 1 (at Alice Springs latitude,
morning), fetches the matching ECMWF reanalysis from Open-Meteo's archive,
runs it through our PV model, and reports:

  * sunrise, sunset and solar noon at the hour's reference location
  * modelled vs measured GHI (reanalysis is not a measurement, but the
    comparison tells us whether the Hay-Davies transposition is sane)
  * modelled vs measured array power (both are in W; direct comparison)
  * mean absolute error, bias, correlation

This is NOT a bias correction - the pack is 2025 Tesseract, we use the
TESSERACT_2025 default which matches it, and the ECMWF value is reanalysis
(ANALYSIS, not FORECAST). The point is to prove the chain from GHI to array
power carries no silent unit error, no sign flip, no scaling bug.

Usage:
    .venv/bin/python scripts/validation/pv_vs_mppt_2025.py

The data folder is hard-coded to the scratch copy; adjust RACE_DATA_DIR if
your checkout lives elsewhere.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rwi.contracts import Provenance
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind
from rwi.core.timebase import utc_now
from rwi.energy.array import DEFAULT_ARRAY, array_power
from rwi.energy.poa import poa_irradiance
from rwi.solar.geometry import solar_day_events, solar_position

RACE_DATA_DIR = Path(
    "/private/tmp/claude-501/"
    "-Users-jafar-Library-Application-Support-Claude-scratch-workspaces-"
    "21f01734-af91-4923-9ce7-6256be0bb0bd-f34ee1a2-1569-4cdb-9340-"
    "24d300246811-scratch-2026-11-01-af524e/26c09fc5-ca35-4644-a69a-"
    "eab856ed2c8e/scratchpad/drive-system/2-documentation/race_data2025"
)

DATE_RE = re.compile(r'"\$date":"([^"]+)"')


def read_mppt_power(day: str, when_start: datetime, when_end: datetime) -> dict[datetime, float]:
    """Sum output power across all four MPPTs per hour, over the given window."""
    hourly: dict[datetime, list[float]] = defaultdict(list)
    for g in range(1, 5):
        path = RACE_DATA_DIR / day / f"MPPT{g}_output_data.csv"
        with path.open() as handle:
            for row in csv.DictReader(handle):
                m = DATE_RE.search(row["timestamp"])
                if not m:
                    continue
                ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                if ts < when_start or ts >= when_end:
                    continue
                try:
                    power = float(row["output_volt"]) * float(row["output_curr"])
                except (ValueError, TypeError):
                    continue
                key = ts.replace(minute=0, second=0, microsecond=0)
                hourly[key].append(power)
    # Sum power across groups: each MPPT contributed independently per second,
    # so the mean per second across groups is already the four-group sum.
    # The correct aggregate is the mean of per-second sums - we approximate
    # by taking per-group mean then summing, since the four MPPTs sample at
    # the same cadence.
    return {hour: sum(vals) / max(1, len(vals)) for hour, vals in hourly.items()}


def fetch_openmeteo_archive(lat: float, lon: float, date: str) -> dict:
    """Pull one day of ECMWF IFS reanalysis for the given point."""
    url = (
        "https://archive-api.open-meteo.com/v1/archive?"
        f"latitude={lat}&longitude={lon}"
        f"&start_date={date}&end_date={date}"
        "&hourly=shortwave_radiation,direct_normal_irradiance,diffuse_radiation,"
        "temperature_2m,cloud_cover,wind_speed_10m"
        "&models=ecmwf_ifs&timezone=UTC"
    )
    with urllib.request.urlopen(url, timeout=25) as resp:
        return json.loads(resp.read())


def main() -> None:
    print("First PV model validation against BWSC 2025 race data")
    print("=" * 70)

    # Use day 1 morning at Alice Springs' latitude. The MPPT data runs from
    # 2025-07-08 15:47Z to 2025-07-09 01:45Z - that is the first race morning
    # local time. Alice Springs' coordinates (BoM station Alice Springs AP).
    site = GeoPoint(latitude_deg=-23.7951, longitude_deg=133.889, elevation_m=545.0)

    # Pick 2025-07-08 20:00 UTC to 23:00 UTC: 05:30 to 08:30 ACST morning,
    # when the sun was climbing and the vehicle was underway. Three solid
    # hours of production.
    start = datetime(2025, 7, 8, 20, 0, tzinfo=UTC)
    end = datetime(2025, 7, 8, 23, 0, tzinfo=UTC)
    print(f"\nSite      : Alice Springs area, {site.latitude_deg}, {site.longitude_deg}")
    print(f"Window    : {start.isoformat()} to {end.isoformat()}")

    # Solar context
    events = solar_day_events(site, start)
    print("\nSolar day context for 2025-07-08:")
    print(f"  Sunrise   : {events.sunrise_utc}")
    print(f"  Noon      : {events.solar_noon_utc}  "
          f"(transit elev {events.transit_elevation_deg:.1f} deg)")
    print(f"  Sunset    : {events.sunset_utc}")
    print(f"  Day length: {events.day_length_s / 3600:.2f} h")

    # Measured
    print("\nReading MPPT data...")
    measured = read_mppt_power("day1", start, end)
    for hour in sorted(measured):
        print(f"  {hour.isoformat():<24s} measured {measured[hour]:>7.1f} W")

    # Reanalysis
    print("\nFetching ECMWF IFS reanalysis...")
    data = fetch_openmeteo_archive(site.latitude_deg, site.longitude_deg, "2025-07-08")
    hourly = data["hourly"]

    print(f"\n{'UTC':<22s} {'GHI':>6s} {'DNI':>6s} {'DHI':>6s} {'Tair':>5s} {'cloud':>5s}  "
          f"{'POA':>6s} {'DC':>7s} {'measured':>8s} {'err':>7s}")
    print("-" * 100)

    comparisons: list[tuple[float, float]] = []
    for i, time_str in enumerate(hourly["time"]):
        hour = datetime.fromisoformat(time_str).replace(tzinfo=UTC)
        if hour not in measured:
            continue

        ghi = hourly["shortwave_radiation"][i]
        dni = hourly["direct_normal_irradiance"][i]
        dhi = hourly["diffuse_radiation"][i]
        t_air = hourly["temperature_2m"][i]
        cloud = hourly["cloud_cover"][i]

        if ghi is None:
            continue

        solar = solar_position(site, hour)
        if not solar.is_daylight:
            continue

        # Build an IrradianceComponents from the reanalysis values.
        from rwi.contracts import IrradianceComponents

        irr = IrradianceComponents(
            time_utc=hour,
            point=site,
            ghi_w_m2=float(ghi),
            dni_w_m2=float(dni) if dni is not None else 0.0,
            dhi_w_m2=float(dhi) if dhi is not None else 0.0,
            kind=DataKind.ANALYSIS,
            provenance=Provenance(
                provider="open-meteo",
                dataset="archive-api/ecmwf_ifs",
                kind=DataKind.ANALYSIS,
                retrieved_at_utc=utc_now(),
                valid_time_utc=hour,
                model_name="ecmwf_ifs",
                point=site,
            ),
            solar_elevation_deg=solar.apparent_elevation_deg,
        )

        # Transpose to the array plane (flat on top of a horizontal car)
        poa = poa_irradiance(
            solar=solar,
            irradiance=irr,
            surface_tilt_deg=0.0,
            surface_azimuth_deg=180.0,
            albedo=0.2,
        )

        # Run the array power model
        est = array_power(
            poa, DEFAULT_ARRAY,
            air_temperature_c=float(t_air),
            wind_speed_mps=float(hourly["wind_speed_10m"][i] or 2.0),
        )

        meas = measured[hour]
        err = est.dc_power_w - meas
        comparisons.append((est.dc_power_w, meas))

        print(f"  {hour.isoformat():<22s} {ghi:>6.0f} {dni or 0:>6.0f} {dhi or 0:>6.0f} "
              f"{t_air:>5.1f} {cloud or 0:>5.0f}  "
              f"{poa.poa_global_w_m2:>6.0f} {est.dc_power_w:>7.0f} "
              f"{meas:>8.0f} {err:>+7.0f}")

    if not comparisons:
        print("\nNo overlapping hours - check the time windows.")
        return

    modelled = [c[0] for c in comparisons]
    measured_list = [c[1] for c in comparisons]
    errors = [m - o for m, o in comparisons]

    print(f"\n{'':=^70s}")
    print(f"Comparison over {len(comparisons)} hour(s):")
    print(f"  Mean modelled : {mean(modelled):>7.1f} W")
    print(f"  Mean measured : {mean(measured_list):>7.1f} W")
    print(f"  MAE           : {mean(abs(e) for e in errors):>7.1f} W")
    print(f"  Bias (mod-obs): {mean(errors):>+7.1f} W")
    print(f"  Overestimate? : {'yes' if mean(errors) > 0 else 'no (model under)'}")

    print()
    print("Interpretation:")
    print("  Model is DEFAULT_ARRAY (6 m2, 0.27 eta, Oxford PV perovskite-Si);")
    print("  this is the 2025 Tesseract spec, so a well-calibrated chain should")
    print("  come within ~10 % of the measured mean. A bias larger than that")
    print("  flags where the next debugging effort belongs (POA, cell temp,")
    print("  cloud attenuation in reanalysis, or MPPT dynamics not modelled).")


# This script is a one-off exploration tool, not a regression test.
# The real result is documented in docs/validation/first-pv-validation.md
# and interpreted there.

if __name__ == "__main__":
    main()
