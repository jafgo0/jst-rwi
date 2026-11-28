"""MERRA-2 hourly surface shortwave (M2T1NXRAD) as an ANALYSIS series (ADR 0017).

MERRA-2 is NASA GMAO's reanalysis: model output constrained by observations, not a
measurement (rule 1). It is used here as a second, independent reanalysis next to ERA5
when both are compared against BSRN Alice Springs.

This module never touches the network. ``scripts/truth/fetch_merra2.py`` calls
``pvlib.iotools.get_merra2`` (NASA Giovanni time-series service, Earthdata login) and
hands the returned ``(frame, meta)`` pair to :func:`hourly_ghi_series`.

Conventions:

* M2T1NXRAD fields are 1-hour means. pvlib returns them stamped at the interval
  midpoint (HH:30). The platform labels a mean at the end of its interval, so the label
  here is the pvlib stamp + 30 min and the value covers (t - 1 h, t].
* ``SWGDN`` (all-sky surface incoming shortwave) becomes ``ghi``. ``SWGDNCLR`` is the
  model's own clear-sky flux; it is not irradiance that reached the ground under the real
  sky, so it is dropped rather than risk it being read as GHI.
* The point is the grid-cell centre pvlib reports; the requested point is kept beside it.
"""

import math
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from rwi.contracts import WeatherSeries, WeatherStep
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance
from rwi.core.variables import unit_for
from rwi.providers.base import classify_value

PROVIDER = "nasa-gmao"
DATASET = "M2T1NXRAD.5.12.4"
MODEL_NAME = "merra2"
# MERRA-2 native grid 0.5 deg latitude x 0.625 deg longitude, about 55 km at 25 S.
SPATIAL_RESOLUTION_KM = 55.0
LICENSE = (
    "NASA open data. Cite GMAO (2015), MERRA-2 tavg1_2d_rad_Nx, Greenbelt, MD, USA, "
    "GES DISC, doi:10.5067/Q9QMY5PBNV1T, and Gelaro et al. (2017), J. Climate 30, 5419-5454."
)
SOURCE_URL = "https://api.giovanni.earthdata.nasa.gov/timeseries"
_GHI_COLUMNS = ("ghi", "SWGDN")


def hourly_ghi_series(
    frame: pd.DataFrame,
    meta: dict[str, Any],
    *,
    requested_point: GeoPoint,
    retrieved_at_utc: datetime,
) -> WeatherSeries:
    """MERRA-2 hourly mean GHI at the grid cell, labelled at the end of the hour (ANALYSIS).

    ``frame``/``meta`` are what ``pvlib.iotools.get_merra2`` returns, with or without
    ``map_variables``. Raises ValueError when the frame has no all-sky shortwave column,
    is not tz-aware, or its stamps are not interval midpoints.
    """
    column = next((c for c in _GHI_COLUMNS if c in frame.columns), None)
    if column is None:
        raise ValueError(f"no all-sky shortwave column ({' or '.join(_GHI_COLUMNS)}) in frame")
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        raise ValueError("MERRA-2 frame index must be tz-aware UTC")
    if len(index) and not ((index.minute == 30) & (index.second == 0)).all():
        raise ValueError("MERRA-2 hourly means must be stamped at HH:30 (interval midpoint)")
    cell = GeoPoint(latitude_deg=float(meta["latitude"]), longitude_deg=float(meta["longitude"]))
    ordered = frame[column].astype(float).sort_index()
    ordered = ordered[~ordered.index.duplicated(keep="first")]
    steps: list[WeatherStep] = []
    for ts, raw in ordered.items():
        t = (pd.Timestamp(ts).tz_convert("UTC") + timedelta(minutes=30)).to_pydatetime()
        mean = None if math.isnan(raw) else round(float(raw), 6)
        value, flag = classify_value("ghi", mean)
        prov = Provenance(
            provider=PROVIDER,
            dataset=DATASET,
            kind=DataKind.ANALYSIS,
            retrieved_at_utc=retrieved_at_utc,
            valid_time_utc=t,
            model_name=MODEL_NAME,
            point=cell,
            requested_point=requested_point,
            spatial_resolution_km=SPATIAL_RESOLUTION_KM,
            temporal_resolution_s=3600.0,
            processing=(
                f"{column} (SWGDN, all-sky surface incoming shortwave), 1-h mean; "
                "pvlib midpoint stamp + 30 min = end of interval"
            ),
            license=LICENSE,
            source_url=SOURCE_URL,
            notes="Reanalysis grid-cell mean, not a measurement.",
        )
        steps.append(
            WeatherStep(
                valid_time_utc=t,
                values={
                    "ghi": Measured(
                        variable="ghi",
                        value=value,
                        unit=unit_for("ghi"),
                        provenance=prov,
                        quality=flag,
                    )
                },
            )
        )
    return WeatherSeries(
        point=cell,
        requested_point=requested_point,
        provider=PROVIDER,
        dataset=DATASET,
        kind=DataKind.ANALYSIS,
        model_name=MODEL_NAME,
        retrieved_at_utc=retrieved_at_utc,
        spatial_resolution_km=SPATIAL_RESOLUTION_KM,
        temporal_resolution_s=3600.0,
        license=LICENSE,
        source_url=SOURCE_URL,
        steps=steps,
    )
