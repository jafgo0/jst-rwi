"""BSRN station-to-archive files: 1-minute GHI/DNI/DHI, quality-controlled (ADR 0017).

The Baseline Surface Radiation Network station at Alice Springs (``asp``) sits on the
Stuart Highway and is the only research-grade irradiance record along the BWSC route.
Files come from the BSRN FTP server (personal account, `scripts/fetch_bsrn.py`); this
module never touches the network. It reads the files with pvlib, applies the BSRN
"physically possible" limits and turns the minutes into hourly observation series.

Conventions:

* LR0100 timestamps are UTC and label the *start* of each 1-minute mean. Solar geometry
  is evaluated at the minute midpoint.
* Hourly means cover (t - 1 h, t] and are labelled t, like the rest of the platform.
* Rule 5: a minute whose apparent solar elevation is <= 0 and that passed the limits is
  set to exactly 0.0 for all three components. A missing night minute stays missing.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pvlib

from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance
from rwi.core.variables import unit_for
from rwi.providers.base import classify_value

PROVIDER = "bsrn"
DATASET = "station-to-archive LR0100"
COMPONENTS: tuple[str, ...] = ("ghi", "dni", "dhi")
LICENSE = (
    "BSRN data release guidelines: scientific use only, no redistribution of the data. "
    "Cite the station and Driemel et al. (2018), Earth Syst. Sci. Data 10, 1491-1513, "
    "doi:10.5194/essd-10-1491-2018."
)
# Long & Shi (2008), The Open Atmospheric Science Journal 2, 23-37, Table 1: the BSRN
# "physically possible" limits. Lower bound -4 W/m2 for all three components.
LOWER_LIMIT_W_M2 = -4.0
_POINT_TOLERANCE_DEG = 1e-3


@dataclass(frozen=True)
class BsrnRecord:
    """Raw 1-minute LR0100 data of one station with its published location."""

    station: str
    station_number: int
    point: GeoPoint
    minutes: pd.DataFrame
    retrieved_at_utc: datetime
    files: tuple[str, ...]


def source_url(station: str) -> str:
    """FTP directory the station-to-archive files of ``station`` come from."""
    return f"ftp://{pvlib.iotools.bsrn.BSRN_FTP_URL}/{station.lower()}/"


def read_bsrn_files(station: str, paths: Sequence[Path]) -> BsrnRecord:
    """Raw LR0100 minutes of ``station`` from monthly files, joined and sorted (OBSERVATION).

    Raises ValueError when a file has no LR0100 record or the files disagree on where
    the station is.
    """
    if not paths:
        raise ValueError("no BSRN files given")
    frames: list[pd.DataFrame] = []
    point: GeoPoint | None = None
    number: int | None = None
    for path in sorted(paths):
        data, meta = pvlib.iotools.read_bsrn(str(path), logical_records=("0100",))
        if data.empty or not meta:
            raise ValueError(f"{path}: no LR0100 (1-minute radiation) record")
        here = GeoPoint(
            latitude_deg=float(meta["latitude"]),
            longitude_deg=float(meta["longitude"]),
            elevation_m=float(meta["altitude"]),
        )
        if point is not None and (
            abs(here.latitude_deg - point.latitude_deg) > _POINT_TOLERANCE_DEG
            or abs(here.longitude_deg - point.longitude_deg) > _POINT_TOLERANCE_DEG
        ):
            raise ValueError(f"{path}: station location {here} differs from {point}")
        point = point or here
        number = number if number is not None else int(meta["station identification number"])
        frames.append(data[list(COMPONENTS)].astype(float))
    joined = pd.concat(frames).sort_index()
    joined = joined[~joined.index.duplicated(keep="first")]
    newest = max(Path(p).stat().st_mtime for p in paths)
    assert point is not None and number is not None
    return BsrnRecord(
        station=station.lower(),
        station_number=number,
        point=point,
        minutes=joined,
        retrieved_at_utc=datetime.fromtimestamp(newest, tz=UTC),
        files=tuple(Path(p).name for p in sorted(paths)),
    )


def physically_possible_limits(
    extra_radiation_w_m2: np.ndarray, cos_zenith: np.ndarray
) -> dict[str, np.ndarray]:
    """BSRN upper limits per component [W/m2] (Long & Shi 2008, Table 1).

    ``cos_zenith`` is clipped at 0, so below the horizon GHI may reach 100 W/m2 and DHI
    50 W/m2 before a minute is rejected, and DNI the extraterrestrial irradiance.
    """
    mu = np.clip(cos_zenith, 0.0, None) ** 1.2
    return {
        "ghi": 1.5 * extra_radiation_w_m2 * mu + 100.0,
        "dhi": 0.95 * extra_radiation_w_m2 * mu + 50.0,
        "dni": np.asarray(extra_radiation_w_m2, dtype=float),
    }


def quality_control(record: BsrnRecord) -> pd.DataFrame:
    """1-minute GHI/DNI/DHI after the BSRN limits and the night rule (OBSERVATION, W/m2).

    Columns: the three components (NaN where missing or rejected) and
    ``apparent_elevation_deg`` from pvlib SPA at the minute midpoint.
    """
    minutes = record.minutes
    mid = minutes.index + pd.Timedelta(seconds=30)
    p = record.point
    solpos = pvlib.solarposition.get_solarposition(
        mid,
        p.latitude_deg,
        p.longitude_deg,
        altitude=p.elevation_m or 0.0,
        method="nrel_numpy",
    )
    s0 = np.asarray(pvlib.irradiance.get_extra_radiation(mid, method="nrel"), dtype=float)
    cos_z = np.cos(np.radians(solpos["zenith"].to_numpy(dtype=float)))
    limits = physically_possible_limits(s0, cos_z)
    night = solpos["apparent_elevation"].to_numpy(dtype=float) <= 0.0
    out = pd.DataFrame(index=minutes.index)
    for c in COMPONENTS:
        x = minutes[c].to_numpy(dtype=float)
        ok = (x > LOWER_LIMIT_W_M2) & (x < limits[c])
        x = np.where(ok, x, np.nan)
        x = np.where(night & ~np.isnan(x), 0.0, x)
        out[c] = x
    out["apparent_elevation_deg"] = solpos["apparent_elevation"].to_numpy(dtype=float)
    return out


def hourly_series(record: BsrnRecord, *, min_minutes: int = 60) -> WeatherSeries:
    """Hourly mean GHI/DNI/DHI at the station, labelled at the end of the hour (OBSERVATION).

    An hour with fewer than ``min_minutes`` valid minutes of a component is MISSING for
    that component. Values pass the registry range check like every other provider.
    """
    if not 1 <= min_minutes <= 60:
        raise ValueError(f"min_minutes must be in 1..60, got {min_minutes!r}")
    qc = quality_control(record)
    grouped = qc[list(COMPONENTS)].resample("1h", label="right", closed="left")
    means, counts = grouped.mean(), grouped.count()
    url = source_url(record.station)
    steps: list[WeatherStep] = []
    for ts in means.index:
        t = ts.to_pydatetime()
        values: dict[str, Measured] = {}
        for c in COMPONENTS:
            n = int(counts.at[ts, c])
            raw = float(means.at[ts, c]) if n >= min_minutes else None
            mean = None if raw is None or math.isnan(raw) else round(raw, 6)
            value, flag = classify_value(c, mean)
            prov = Provenance(
                provider=PROVIDER,
                dataset=DATASET,
                kind=DataKind.OBSERVATION,
                retrieved_at_utc=record.retrieved_at_utc,
                observation_time_utc=t,
                point=record.point,
                temporal_resolution_s=3600.0,
                processing=(
                    f"mean of {n}/60 1-min values over (t-1h, t]; BSRN physically possible "
                    f"limits (Long & Shi 2008); apparent elevation <= 0 set to 0.0; "
                    f"needs >= {min_minutes} valid minutes"
                ),
                license=LICENSE,
                source_url=url,
                notes=(
                    f"BSRN station {record.station} (no. {record.station_number}), "
                    f"files {', '.join(record.files)}. Pyranometer/pyrheliometer measurement."
                ),
            )
            values[c] = Measured(
                variable=c, value=value, unit=unit_for(c), provenance=prov, quality=flag
            )
        steps.append(WeatherStep(valid_time_utc=t, values=values))
    return WeatherSeries(
        point=record.point,
        provider=PROVIDER,
        dataset=DATASET,
        kind=DataKind.OBSERVATION,
        retrieved_at_utc=record.retrieved_at_utc,
        temporal_resolution_s=3600.0,
        license=LICENSE,
        source_url=url,
        steps=steps,
    )
