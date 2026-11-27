"""CAMS global aerosol forecast through the Open-Meteo Air Quality API (ADR 0014).

Variables: total aerosol optical depth at 550 nm (dimensionless) and dust
surface concentration (ug/m3), hourly, from the ``cams_global`` domain
(0.4 deg, ECMWF CAMS). Open-Meteo gives no Angstrom exponent and no AOD at other
wavelengths; `rwi.solar.turbidity` converts AOD550 with a configured exponent.

This adapter is not in the weather-provider registry: it does not serve the
`WeatherSeries` variable set, only aerosol, so it has its own small model.
"""

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from rwi.contracts import ProviderCapabilities
from rwi.core.errors import ProviderUnavailableError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, QualityFlag
from rwi.core.timebase import TimeWindow, ensure_utc
from rwi.providers.base import BaseProvider, ceil_hour, floor_hour, make_cache_key

__all__ = [
    "CAMS_DOMAIN",
    "CAMS_LICENSE",
    "AerosolSeries",
    "AerosolStep",
    "CamsAerosolProvider",
]

CAMS_DOMAIN = "cams_global"
CAMS_RESOLUTION_KM = 45.0  # 0.4 deg CAMS global grid
CAMS_LICENSE = (
    "Copernicus Atmosphere Monitoring Service information (CC BY 4.0, via Open-Meteo); "
    "neither the European Commission nor ECMWF is responsible for its use"
)
_API_VARS = ("aerosol_optical_depth", "dust")
_UNITS = {"aod550": "1", "dust_ug_m3": "ug/m3"}


class AerosolStep(BaseModel):
    """CAMS aerosol at one valid time; a None value is missing upstream."""

    model_config = ConfigDict(frozen=True)

    valid_time_utc: datetime
    aod550: float | None = Field(default=None, description="total AOD at 550 nm, dimensionless")
    dust_ug_m3: float | None = Field(default=None, description="dust at the surface, ug/m3")
    aod550_quality: QualityFlag = QualityFlag.MISSING
    dust_quality: QualityFlag = QualityFlag.MISSING


class AerosolSeries(BaseModel):
    """Hourly CAMS aerosol at one grid point, with provenance and units."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    point: GeoPoint
    requested_point: GeoPoint
    model_name: str = CAMS_DOMAIN
    units: dict[str, str] = Field(default_factory=lambda: dict(_UNITS))
    steps: list[AerosolStep]
    provenance: Provenance


def _fmt_hour(when: datetime) -> str:
    return ensure_utc(when).strftime("%Y-%m-%dT%H:%M")


def _value(raw: Any, *, upper: float) -> tuple[float | None, QualityFlag]:
    if raw is None or isinstance(raw, bool):
        return None, QualityFlag.MISSING
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None, QualityFlag.MISSING
    if not math.isfinite(v) or v < 0.0 or v > upper:
        return None, QualityFlag.OUT_OF_RANGE
    return v, QualityFlag.OK


# Physical plausibility bounds, not tuning: AOD550 above 10 and surface dust
# above 1e5 ug/m3 are beyond any observed event (severe dust storms reach ~1e4).
_AOD_MAX = 10.0
_DUST_MAX = 1.0e5


class CamsAerosolProvider(BaseProvider):
    """Open-Meteo Air Quality adapter for CAMS AOD550 and dust (keyless)."""

    name = "open-meteo-air-quality"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            datasets=("air-quality",),
            supports_observations=False,
            supports_forecast=True,
            supports_ensemble=False,
            supports_archive=False,
            variables=frozenset(),
            max_forecast_hours=None,
            spatial_resolution_km=CAMS_RESOLUTION_KM,
            update_interval_s=None,
            license=CAMS_LICENSE,
            attribution="Copernicus Atmosphere Monitoring Service (CAMS), via Open-Meteo",
            documentation_url="https://open-meteo.com/en/docs/air-quality-api",
            requires_api_key=False,
            notes="CAMS global aerosol (AOD550, dust); not a WeatherSeries provider",
        )

    async def _probe(self) -> None:
        now = self._now()
        await self.fetch_aerosol(GeoPoint(latitude_deg=0.0, longitude_deg=0.0), now, now)

    async def fetch_aerosol(
        self, point: GeoPoint, start_utc: datetime, end_utc: datetime
    ) -> AerosolSeries:
        """Hourly CAMS AOD550 and dust at ``point`` for the hours covering the window (FORECAST)."""
        first, last = floor_hour(ensure_utc(start_utc)), ceil_hour(ensure_utc(end_utc))
        if last < first:
            raise ValueError("end_utc is before start_utc")
        url = f"{self.settings.openmeteo_air_quality_url.rstrip('/')}/air-quality"
        params: dict[str, Any] = {
            "latitude": round(point.latitude_deg, 5),
            "longitude": round(point.longitude_deg, 5),
            "hourly": ",".join(_API_VARS),
            "domains": CAMS_DOMAIN,
            "timezone": "UTC",
            "start_hour": _fmt_hour(first),
            "end_hour": _fmt_hour(last),
        }
        key = make_cache_key(
            provider=self.name,
            dataset="air-quality",
            point=point,
            window=TimeWindow(start_utc=first, end_utc=last),
            variables=list(_API_VARS),
            extra={"domains": CAMS_DOMAIN},
        )

        async def fetch() -> dict[str, Any]:
            data = await self._get_json(url, params)
            self._validate(data)
            return {"request_url": str(httpx.URL(url, params=params)), "response": data}

        fetched = await self._cached(key, self._ttl_s(DataKind.FORECAST), fetch)
        return self._series(point, fetched.payload, fetched.retrieved_at_utc, fetched.age_s)

    @staticmethod
    def _validate(data: Any) -> None:
        if not isinstance(data, Mapping):
            raise ProviderUnavailableError("open-meteo-air-quality: response is not an object")
        if data.get("error"):
            raise ProviderUnavailableError(
                f"open-meteo-air-quality: {str(data.get('reason', 'error'))[:300]}"
            )
        if data.get("utc_offset_seconds", 0) != 0:
            raise ProviderUnavailableError("open-meteo-air-quality: response is not in UTC")
        hourly = data.get("hourly")
        if not isinstance(hourly, Mapping) or not isinstance(hourly.get("time"), list):
            raise ProviderUnavailableError("open-meteo-air-quality: no hourly block")
        n = len(hourly["time"])
        for var in _API_VARS:
            col = hourly.get(var)
            if not isinstance(col, list) or len(col) != n:
                raise ProviderUnavailableError(
                    f"open-meteo-air-quality: hourly.{var} missing or of the wrong length"
                )

    def _series(
        self, requested: GeoPoint, blob: Mapping[str, Any], retrieved: datetime, age_s: float
    ) -> AerosolSeries:
        data = blob["response"]
        self._validate(data)
        hourly = data["hourly"]
        steps: list[AerosolStep] = []
        for i, t in enumerate(hourly["time"]):
            parsed = datetime.fromisoformat(str(t))
            when = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else ensure_utc(parsed)
            aod, aod_q = _value(hourly["aerosol_optical_depth"][i], upper=_AOD_MAX)
            dust, dust_q = _value(hourly["dust"][i], upper=_DUST_MAX)
            steps.append(
                AerosolStep(
                    valid_time_utc=when,
                    aod550=aod,
                    dust_ug_m3=dust,
                    aod550_quality=aod_q,
                    dust_quality=dust_q,
                )
            )
        grid = GeoPoint(
            latitude_deg=float(data.get("latitude", requested.latitude_deg)),
            longitude_deg=float(data.get("longitude", requested.longitude_deg)),
        )
        provenance = Provenance(
            provider="open-meteo",
            dataset="air-quality",
            kind=DataKind.FORECAST,
            retrieved_at_utc=retrieved,
            valid_time_utc=steps[0].valid_time_utc if steps else None,
            model_name=CAMS_DOMAIN,
            point=grid,
            requested_point=requested,
            spatial_resolution_km=CAMS_RESOLUTION_KM,
            temporal_resolution_s=3600.0,
            license=CAMS_LICENSE,
            source_url=str(blob.get("request_url", "")) or None,
            notes=(
                f"hourly AOD550 [1] and dust [ug/m3]; cache age {age_s:.0f} s; "
                "values outside physical bounds are dropped as out_of_range"
            ),
        )
        return AerosolSeries(
            point=grid, requested_point=requested, steps=steps, provenance=provenance
        )
