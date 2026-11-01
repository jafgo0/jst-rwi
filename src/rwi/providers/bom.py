"""Australian Bureau of Meteorology (BoM) station observations.

What this adapter is, honestly (verified 2026-11-01, see docs/science/providers.md):

* The BoM "Observations - Australia - 72-hour history" product (IDx60910; one JSON
  file per station, half-hourly, ~72 h) is the only free, documented source of
  true station *measurements* for the Darwin-Adelaide route. The user guide
  (https://www.bom.gov.au/catalogue/72_hr_historical_obs.pdf, v3.2) documents the
  fields; the files are served anonymously at
  ``https://www.bom.gov.au/fwo/<IDx60910>/<IDx60910>.<WMO>.json``.
* **Licence restriction** (https://www.bom.gov.au/catalogue/data-feeds.shtml and
  https://www.bom.gov.au/other/copyright.shtml): free anonymous products are for
  personal use or use *within your organisation*; they may not be supplied to
  other people or used commercially, and the Bureau gives no availability
  guarantee. Publishing BoM data (e.g. a public dashboard) requires Registered
  User access / a data licence. This adapter is therefore suitable for the team's
  internal tool only.
* It provides NO irradiance, NO forecasts, NO precipitation mapping (see below).

Kind returned: ``DataKind.OBSERVATION`` - these are instrument measurements.
Forecasts raise `CapabilityNotSupportedError`.

Not mapped on purpose: ``rain_trace`` (accumulation "since 9 am"), ``rain_ten`` and
``rain_hour`` (overlapping windows that do not match the registry's "per step"
precipitation) and ``press``/``press_qnh`` (MSLP *or* QNH, not station pressure).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from rwi.config import Settings
from rwi.contracts import ProviderCapabilities, WeatherSeries, WeatherStep
from rwi.core.errors import (
    CapabilityNotSupportedError,
    ProviderUnavailableError,
)
from rwi.core.geo import GeoPoint, geodesic_distance_m
from rwi.core.provenance import DataKind, Measured, Provenance
from rwi.core.timebase import TimeWindow, ensure_utc
from rwi.core.units import hpa_to_pa, kmh_to_ms
from rwi.core.variables import unit_for
from rwi.providers.base import BaseProvider, classify_value

__all__ = ["BOM_LICENSE", "BOM_STATIONS", "BomProvider", "BomStation"]

PROVIDER_NAME = "bom"

BOM_LICENSE = (
    "(c) Commonwealth of Australia, Bureau of Meteorology. Free anonymous product: personal use "
    "or use within your organisation only; must not be supplied to any other person or used for "
    "any commercial purpose; no availability guarantee. Publishing requires Registered User "
    "access / data licence (https://www.bom.gov.au/catalogue/data-feeds.shtml, "
    "https://www.bom.gov.au/other/copyright.shtml, verified 2026-11-01)."
)
BOM_ATTRIBUTION = "Source: Australian Bureau of Meteorology (c) Commonwealth of Australia."

DOC_URL = "https://www.bom.gov.au/catalogue/72_hr_historical_obs.pdf"
NATIVE_STEP_S = 1800.0


@dataclass(frozen=True)
class BomStation:
    """A station verified present in the IDx60910 feed on 2026-11-01."""

    wmo: int
    name: str
    product: str  # e.g. IDD60910 (D = Northern Territory, S = South Australia)
    latitude_deg: float
    longitude_deg: float
    elevation_m: float

    @property
    def point(self) -> GeoPoint:
        return GeoPoint(
            latitude_deg=self.latitude_deg,
            longitude_deg=self.longitude_deg,
            elevation_m=self.elevation_m,
        )


# Coordinates/heights from the state observation XML (IDD60920/IDS60920, anonymous FTP);
# each file was fetched successfully from the /fwo JSON path on 2026-11-01.
BOM_STATIONS: tuple[BomStation, ...] = (
    BomStation(94120, "Darwin Airport", "IDD60910", -12.4239, 130.8925, 30.40),
    BomStation(94234, "Daly Waters", "IDD60910", -16.2637, 133.3782, 209.84),
    BomStation(94238, "Tennant Creek Airport", "IDD60910", -19.6423, 134.1833, 375.70),
    BomStation(94326, "Alice Springs Airport", "IDD60910", -23.7951, 133.8890, 546.00),
    BomStation(95458, "Coober Pedy Airport", "IDS60910", -29.0347, 134.7222, 225.00),
    BomStation(94659, "Woomera Aerodrome", "IDS60910", -31.1558, 136.8054, 166.60),
    BomStation(95666, "Port Augusta Aero", "IDS60910", -32.5073, 137.7169, 13.97),
    BomStation(94648, "Adelaide (West Terrace)", "IDS60910", -34.9257, 138.5832, 29.32),
    BomStation(94146, "Adelaide Airport", "IDS60910", -34.9502, 138.5342, 3.31),
)

_Conv = Callable[[float], float]


def _num(raw: Any) -> float | None:
    """Parse a BoM JSON scalar: numbers, numeric strings; '-' / '' / None -> None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw) if math.isfinite(float(raw)) else None
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text == "-":
            return None
        try:
            v = float(text)
        except ValueError:
            return None
        return v if math.isfinite(v) else None
    return None


def _identity(v: float) -> float:
    return v


def _oktas_to_percent(v: float) -> float | None:
    # 0-8 oktas are cover; BoM code 9 means sky obscured (fog), which is not a cover fraction.
    return v * 12.5 if 0.0 <= v <= 8.0 else None


def _km_to_m(v: float) -> float:
    return v * 1000.0


# canonical -> (feed field, converter, note)
_FIELDS: dict[str, tuple[str, _Conv, str]] = {
    "temperature_2m": ("air_temp", _identity, ""),
    "apparent_temperature": ("apparent_t", _identity, "Steadman apparent temperature"),
    "dew_point_2m": ("dewpt", _identity, ""),
    "relative_humidity_2m": ("rel_hum", _identity, ""),
    "pressure_msl": ("press_msl", hpa_to_pa, ""),
    "wind_speed_10m": ("wind_spd_kmh", kmh_to_ms, "mean over 1 min (OMD) or 10 min"),
    "wind_gusts_10m": ("gust_kmh", kmh_to_ms, "highest 3-s mean; absent for synop/mdf"),
    "wind_direction_10m": ("wind_dir_deg", _identity, "calm -> missing (feed prints 0)"),
    "cloud_cover": ("cloud_oktas", _identity, "oktas x 12.5; code 9 (obscured) -> missing"),
    "visibility": ("vis_km", _km_to_m, "km in feed, converted to m"),
}
SUPPORTED_VARIABLES = frozenset(_FIELDS)


class BomProvider(BaseProvider):
    """BoM station observations (free anonymous HTTP JSON; internal use only)."""

    name = PROVIDER_NAME

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        stations: Sequence[BomStation] = BOM_STATIONS,
        max_station_distance_km: float = 100.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(settings, **kwargs)
        self.stations = tuple(stations)
        self.max_station_distance_km = max_station_distance_km

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            datasets=("station-observations-72h",),
            supports_observations=True,
            supports_forecast=False,
            supports_ensemble=False,
            supports_archive=False,
            variables=SUPPORTED_VARIABLES,
            max_forecast_hours=None,
            spatial_resolution_km=None,
            update_interval_s=1800.0,
            license=BOM_LICENSE,
            attribution=BOM_ATTRIBUTION,
            documentation_url=DOC_URL,
            requires_api_key=False,
            notes=(
                "Point measurements at the nearest of the configured stations (not interpolated); "
                "72 h history, half-hourly, updated ~x:05 and x:35. No irradiance, no forecasts. "
                "Free anonymous access is for personal / within-organisation use only and carries "
                "no availability guarantee; public display requires BoM Registered User "
                "access. Only the stations in BOM_STATIONS are configured."
            ),
        )

    # -- station selection ------------------------------------------------------

    def nearest_station(self, point: GeoPoint) -> tuple[BomStation, float]:
        """(station, distance_km) of the closest configured station."""
        if not self.stations:
            raise CapabilityNotSupportedError("bom: no stations configured")
        best = min(self.stations, key=lambda s: geodesic_distance_m(point, s.point))
        return best, geodesic_distance_m(point, best.point) / 1000.0

    def _feed_url(self, station: BomStation) -> str:
        base = self.settings.bom_ftp_base.rstrip("/")
        return f"{base}/{station.product}/{station.product}.{station.wmo}.json"

    async def _probe(self) -> None:
        station = self.stations[0]
        data = await self._get_json(self._feed_url(station))
        self._records(data)

    # -- protocol methods -------------------------------------------------------

    async def fetch_forecast(
        self, point: GeoPoint, window: TimeWindow, variables: Sequence[str]
    ) -> WeatherSeries:
        raise CapabilityNotSupportedError(
            "bom: this adapter serves station observations only; the Bureau's forecast products "
            "need a separate (licensed) integration"
        )

    async def fetch_observations(
        self,
        point: GeoPoint,
        window: TimeWindow,
        variables: Sequence[str],
        *,
        station_wmo: int | None = None,
    ) -> WeatherSeries:
        """Measured values at the nearest station, ascending in time (``kind=OBSERVATION``).

        Returns every record with ``window.start <= t <= window.end`` (inclusive)
        from the ~72 h feed. ``point`` is the *station* location, ``requested_point``
        the caller's; the distance is stated in each value's provenance notes.
        Raises `CapabilityNotSupportedError` when no configured station is within
        ``max_station_distance_km``.
        """
        vars_ = self._check_variables(variables)
        if station_wmo is not None:
            matches = [s for s in self.stations if s.wmo == station_wmo]
            if not matches:
                raise CapabilityNotSupportedError(f"bom: station {station_wmo} is not configured")
            station = matches[0]
            distance_km = geodesic_distance_m(point, station.point) / 1000.0
        else:
            station, distance_km = self.nearest_station(point)
            if distance_km > self.max_station_distance_km:
                raise CapabilityNotSupportedError(
                    f"bom: nearest station ({station.name}) is {distance_km:.0f} km away, beyond "
                    f"the {self.max_station_distance_km:g} km limit; refusing to present it as "
                    "weather at this point"
                )

        url = self._feed_url(station)
        key = {
            "provider": self.name,
            "dataset": "station-observations-72h",
            "station": station.wmo,
            "product": station.product,
        }

        async def fetch() -> dict[str, Any]:
            data = await self._get_json(url)
            self._records(data)  # validate shape before caching
            return {"request_url": url, "response": data}

        fetched = await self._cached(key, self._ttl_s(DataKind.OBSERVATION), fetch)
        return self._build_series(
            fetched.payload["response"],
            station,
            distance_km,
            point,
            window,
            vars_,
            url,
            fetched.retrieved_at_utc,
            fetched.from_cache,
            fetched.age_s,
        )

    # -- parsing ----------------------------------------------------------------

    @staticmethod
    def _records(data: Any) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
        try:
            obs = data["observations"]
            header = obs["header"][0]
            records = obs["data"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailableError(
                "bom: response is not a Bureau observations JSON document"
            ) from exc
        if not isinstance(records, list):
            raise ProviderUnavailableError("bom: observations.data is not a list")
        return header, records

    @staticmethod
    def _parse_time(text: Any) -> datetime | None:
        if not isinstance(text, str):
            return None
        try:
            return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            return None

    def _build_series(
        self,
        data: Any,
        station: BomStation,
        distance_km: float,
        requested: GeoPoint,
        window: TimeWindow,
        variables: list[str],
        url: str,
        retrieved: datetime,
        from_cache: bool,
        age_s: float,
    ) -> WeatherSeries:
        header, records = self._records(data)
        start, end = ensure_utc(window.start_utc), ensure_utc(window.end_utc)

        parsed: dict[datetime, Mapping[str, Any]] = {}
        for rec in records:
            if not isinstance(rec, Mapping):
                continue
            t = self._parse_time(rec.get("aifstime_utc"))
            if t is not None and t not in parsed:
                parsed[t] = rec
        if not parsed:
            raise ProviderUnavailableError("bom: feed contains no timestamped records")
        selected = sorted(t for t in parsed if start <= t <= end)
        if not selected:
            raise ProviderUnavailableError(
                f"bom: no observations for {station.name} between {start.isoformat()} and "
                f"{end.isoformat()}; the feed covers {min(parsed).isoformat()} to "
                f"{max(parsed).isoformat()}"
            )

        # station location as published in the record (fallback: catalogue)
        first = parsed[selected[0]]
        lat, lon = _num(first.get("lat")), _num(first.get("lon"))
        try:
            station_point = GeoPoint(
                latitude_deg=lat if lat is not None else station.latitude_deg,
                longitude_deg=lon if lon is not None else station.longitude_deg,
                elevation_m=station.elevation_m,
            )
        except ValidationError as exc:
            raise ProviderUnavailableError(f"bom: invalid station location: {exc}") from exc

        diffs = [(b - a).total_seconds() for a, b in itertools.pairwise(selected)]
        temporal_s = min(diffs) if diffs else NATIVE_STEP_S

        steps: list[WeatherStep] = []
        for t in selected:
            rec = parsed[t]
            wind_src = rec.get("wind_src") or "unknown"
            notes = (
                f"Station measurement, not interpolated: {header.get('name', station.name)} "
                f"(WMO {station.wmo}), {distance_km:.1f} km from the requested point; product "
                f"{station.product}; wind_src={wind_src}. No QC status is published in this feed. "
                "Free-tier licence: internal use only."
                + (f" Served from local cache ({age_s:.0f} s old)." if from_cache else "")
            )
            prov = Provenance(
                provider=self.name,
                dataset="station-observations-72h",
                kind=DataKind.OBSERVATION,
                retrieved_at_utc=retrieved,
                observation_time_utc=t,
                point=station_point,
                requested_point=requested,
                temporal_resolution_s=temporal_s,
                processing="none (raw station report; unit conversion only)",
                license=BOM_LICENSE,
                source_url=url,
                notes=notes,
            )
            values: dict[str, Measured] = {}
            for v in variables:
                field, conv, _ = _FIELDS[v]
                raw = rec.get(field)
                number = _num(raw)
                if v == "wind_direction_10m" and str(rec.get("wind_dir", "")).upper() == "CALM":
                    number = None
                converted: float | None
                if number is None:
                    converted = None
                elif v == "cloud_cover":
                    converted = _oktas_to_percent(number)
                else:
                    converted = round(conv(number), 9)
                value, flag = classify_value(v, converted)
                values[v] = Measured(
                    variable=v, value=value, unit=unit_for(v), provenance=prov, quality=flag
                )
            steps.append(WeatherStep(valid_time_utc=t, values=values))

        return WeatherSeries(
            point=station_point,
            requested_point=requested,
            provider=self.name,
            dataset="station-observations-72h",
            kind=DataKind.OBSERVATION,
            model_name=None,
            retrieved_at_utc=retrieved,
            temporal_resolution_s=temporal_s,
            license=BOM_LICENSE,
            source_url=url,
            steps=steps,
        )
