"""NASA FIRMS active-fire hotspots over a bounding box (ADR 0014).

Area API: ``{base}/api/area/csv/{MAP_KEY}/{SOURCE}/{west,south,east,north}/{DAY_RANGE}``,
CSV out. The MAP_KEY sits in the URL *path*, so this adapter does not use
`BaseProvider._get_json` (whose messages name the URL): every URL it logs,
raises, caches or puts in provenance has the key replaced by ``***``. The key
is never part of the cache key. Keep the ``httpx`` logger at WARNING or above;
at INFO httpx logs full request URLs (`quiet_httpx_logger` does that).

Hotspots are satellite detections (``DataKind.OBSERVATION``) in near real time:
not validated, so each carries ``QualityFlag.UNVALIDATED``. A hotspot is a
thermal anomaly in a 375 m (VIIRS) or 1 km (MODIS) pixel, not a fire perimeter.
"""

import csv
import io
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from rwi.config import Settings
from rwi.contracts import ProviderCapabilities
from rwi.core.errors import ProviderError, ProviderRateLimitError, ProviderUnavailableError
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, QualityFlag
from rwi.core.timebase import TimeWindow
from rwi.providers.base import BaseProvider, floor_hour, make_cache_key
from rwi.providers.gridded import BBox

__all__ = [
    "FIRMS_LICENSE",
    "FireHotspot",
    "FirmsProvider",
    "HotspotSet",
    "firms_missing_config",
    "parse_firms_csv",
    "quiet_httpx_logger",
]

log = logging.getLogger("rwi.providers")

Confidence = Literal["low", "nominal", "high"]
CONFIDENCE_RANK: dict[str, int] = {"low": 0, "nominal": 1, "high": 2}
FIRMS_LICENSE = (
    "NASA FIRMS (LANCE), open data; acknowledge 'NASA Fire Information for Resource "
    "Management System (FIRMS)'"
)
_MASK = "***"
_HEADER = "latitude,longitude"
# MODIS confidence is 0-100 %; FIRMS documents the classes low < 30 <= nominal < 80 <= high
# (Giglio et al., MODIS Collection 6 Active Fire Product User's Guide, sect. 4.5).
_MODIS_NOMINAL_PCT = 30.0
_MODIS_HIGH_PCT = 80.0
_VIIRS_CONFIDENCE = {"l": "low", "n": "nominal", "h": "high"}


class FireHotspot(BaseModel):
    """One FIRMS detection: where, when, how hot and how sure."""

    model_config = ConfigDict(frozen=True)

    point: GeoPoint
    acquired_utc: datetime
    satellite: str
    instrument: str
    confidence: Confidence
    frp_mw: float | None
    """Fire radiative power [MW]; None when the row has none."""
    brightness_k: float | None
    """I-4 (VIIRS) or band-21 (MODIS) brightness temperature [K]."""
    daynight: str
    quality: QualityFlag = QualityFlag.UNVALIDATED


class HotspotSet(BaseModel):
    """FIRMS hotspots in a bbox over the last ``day_range`` days, with provenance."""

    model_config = ConfigDict(frozen=True)

    source: str
    day_range: int
    bbox: tuple[float, float, float, float]
    """west, south, east, north [deg]."""
    hotspots: list[FireHotspot]
    rows_rejected: int
    units: dict[str, str] = {"frp_mw": "MW", "brightness_k": "K"}
    provenance: Provenance


def firms_missing_config(settings: Settings) -> list[str]:
    """Names of the RWI_FIRMS_* settings that must be set before FIRMS is queried."""
    missing = []
    if settings.firms_map_key is None or not settings.firms_map_key.get_secret_value().strip():
        missing.append("RWI_FIRMS_MAP_KEY")
    if not settings.firms_source:
        missing.append("RWI_FIRMS_SOURCE")
    if settings.firms_day_range is None:
        missing.append("RWI_FIRMS_DAY_RANGE")
    if settings.firms_route_buffer_km is None:
        missing.append("RWI_FIRMS_ROUTE_BUFFER_KM")
    return missing


def quiet_httpx_logger() -> None:
    """Raise the httpx logger to WARNING so request URLs (which carry the key) are not logged."""
    lg = logging.getLogger("httpx")
    if lg.getEffectiveLevel() < logging.WARNING:
        lg.setLevel(logging.WARNING)


def _confidence(raw: str) -> Confidence | None:
    text = raw.strip().lower()
    if text in CONFIDENCE_RANK:
        return text  # type: ignore[return-value]
    if text in _VIIRS_CONFIDENCE:
        return _VIIRS_CONFIDENCE[text]  # type: ignore[return-value]
    try:
        pct = float(text)
    except ValueError:
        return None
    if not math.isfinite(pct) or not 0.0 <= pct <= 100.0:
        return None
    if pct >= _MODIS_HIGH_PCT:
        return "high"
    return "nominal" if pct >= _MODIS_NOMINAL_PCT else "low"


def _opt_float(raw: str | None) -> float | None:
    try:
        v = float(raw) if raw not in (None, "") else None
    except ValueError:
        return None
    return v if v is not None and math.isfinite(v) else None


def parse_firms_csv(text: str) -> tuple[list[FireHotspot], int]:
    """Hotspots from a FIRMS area CSV and the number of rows that could not be used."""
    reader = csv.DictReader(io.StringIO(text))
    fields = set(reader.fieldnames or [])
    need = {"latitude", "longitude", "acq_date", "acq_time", "confidence"}
    if not need <= fields:
        raise ProviderUnavailableError(f"nasa-firms: CSV lacks columns {sorted(need - fields)}")
    out: list[FireHotspot] = []
    rejected = 0
    for row in reader:
        try:
            lat, lon = float(row["latitude"]), float(row["longitude"])
            hhmm = row["acq_time"].strip().zfill(4)
            when = datetime.strptime(f"{row['acq_date'].strip()} {hhmm}", "%Y-%m-%d %H%M")
            conf = _confidence(row["confidence"])
            if conf is None:
                raise ValueError("confidence")
            point = GeoPoint(latitude_deg=lat, longitude_deg=lon)
        except (ValueError, KeyError, AttributeError):
            rejected += 1
            continue
        frp = _opt_float(row.get("frp"))
        bright = _opt_float(row.get("bright_ti4") or row.get("brightness"))
        out.append(
            FireHotspot(
                point=point,
                acquired_utc=when.replace(tzinfo=UTC),
                satellite=(row.get("satellite") or "").strip(),
                instrument="VIIRS" if "bright_ti4" in fields else "MODIS",
                confidence=conf,
                frp_mw=frp if frp is not None and frp >= 0.0 else None,
                brightness_k=bright,
                daynight=(row.get("daynight") or "").strip(),
            )
        )
    return out, rejected


class FirmsProvider(BaseProvider):
    """NASA FIRMS area API adapter; requires a MAP_KEY and never logs it."""

    name = "nasa-firms"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            datasets=(self.settings.firms_source or "unconfigured",),
            supports_observations=True,
            supports_forecast=False,
            supports_ensemble=False,
            supports_archive=False,
            variables=frozenset(),
            max_forecast_hours=None,
            spatial_resolution_km=0.375,
            update_interval_s=None,
            license=FIRMS_LICENSE,
            attribution="NASA FIRMS",
            documentation_url="https://firms.modaps.eosdis.nasa.gov/api/area/",
            requires_api_key=True,
            notes="active-fire hotspots; not a WeatherSeries provider",
        )

    def _key(self) -> str:
        missing = firms_missing_config(self.settings)
        if missing:
            raise ProviderUnavailableError(f"nasa-firms: not configured, set {', '.join(missing)}")
        assert self.settings.firms_map_key is not None
        return self.settings.firms_map_key.get_secret_value().strip()

    def _urls(self, bbox: BBox) -> tuple[str, str]:
        key = self._key()
        day_range = int(self.settings.firms_day_range or 0)
        if not 1 <= day_range <= 5:
            raise ValueError(f"RWI_FIRMS_DAY_RANGE must be 1..5, got {day_range}")
        area = f"{bbox.west:.4f},{bbox.south:.4f},{bbox.east:.4f},{bbox.north:.4f}"
        tail = f"{self.settings.firms_source}/{area}/{day_range}"
        base = f"{self.settings.firms_base_url.rstrip('/')}/api/area/csv"
        return f"{base}/{key}/{tail}", f"{base}/{_MASK}/{tail}"

    def _scrub(self, text: str) -> str:
        key = self.settings.firms_map_key
        secret = key.get_secret_value().strip() if key is not None else ""
        return text.replace(secret, _MASK) if secret else text

    async def _get_text(self, url: str, masked: str) -> str:
        quiet_httpx_logger()
        attempts = max(0, int(self.settings.http_retries)) + 1
        failure: ProviderError = ProviderUnavailableError(f"{self.name}: no attempt made")
        for attempt in range(attempts):
            try:
                response = await self._http().get(url)
            except httpx.TimeoutException:
                failure = ProviderUnavailableError(f"{self.name}: timeout calling {masked}")
            except httpx.TransportError as exc:
                failure = ProviderUnavailableError(
                    f"{self.name}: network error calling {masked}: {type(exc).__name__}"
                )
            else:
                body = self._scrub(response.text[:300].strip())
                if response.status_code == 429:
                    failure = ProviderRateLimitError(f"{self.name}: rate limited by {masked}")
                elif response.status_code >= 500:
                    failure = ProviderUnavailableError(
                        f"{self.name}: HTTP {response.status_code} from {masked}: {body}"
                    )
                elif response.status_code != 200:
                    raise ProviderUnavailableError(
                        f"{self.name}: request rejected, HTTP {response.status_code} from "
                        f"{masked}: {body}"
                    )
                elif not response.text.lstrip().lower().startswith(_HEADER):
                    # FIRMS answers some key/quota errors with HTTP 200 and a plain message.
                    raise ProviderUnavailableError(
                        f"{self.name}: {masked} did not return hotspot CSV: {body[:120]}"
                    )
                else:
                    return response.text
            if attempt < attempts - 1:
                await self._sleep(self.settings.http_backoff_s * (2**attempt))
        raise failure

    async def _probe(self) -> None:
        self._key()

    async def fetch_hotspots(self, bbox: BBox) -> HotspotSet:
        """FIRMS hotspots in ``bbox`` over the configured day range (OBSERVATION, unvalidated)."""
        url, masked = self._urls(bbox)
        now = floor_hour(self._now())
        day_range = int(self.settings.firms_day_range or 0)
        centre = GeoPoint(
            latitude_deg=(bbox.south + bbox.north) / 2.0,
            longitude_deg=(bbox.west + bbox.east) / 2.0,
        )
        key = make_cache_key(
            provider=self.name,
            dataset=str(self.settings.firms_source),
            point=centre,
            window=TimeWindow(start_utc=now - timedelta(days=day_range), end_utc=now),
            variables=["hotspots"],
            extra={"bbox": [bbox.west, bbox.south, bbox.east, bbox.north], "days": day_range},
        )

        async def fetch() -> dict[str, Any]:
            return {"request_url": masked, "csv": await self._get_text(url, masked)}

        fetched = await self._cached(key, self._ttl_s(DataKind.OBSERVATION), fetch)
        hotspots, rejected = parse_firms_csv(str(fetched.payload["csv"]))
        latest = max((h.acquired_utc for h in hotspots), default=None)
        provenance = Provenance(
            provider=self.name,
            dataset=str(self.settings.firms_source),
            kind=DataKind.OBSERVATION,
            retrieved_at_utc=fetched.retrieved_at_utc,
            observation_time_utc=latest,
            point=centre,
            spatial_resolution_km=0.375 if "VIIRS" in str(self.settings.firms_source) else 1.0,
            license=FIRMS_LICENSE,
            source_url=str(fetched.payload.get("request_url", masked)),
            notes=(
                f"{len(hotspots)} hotspots over {day_range} d, {rejected} rows rejected; "
                f"near-real-time, unvalidated; cache age {fetched.age_s:.0f} s"
            ),
        )
        return HotspotSet(
            source=str(self.settings.firms_source),
            day_range=day_range,
            bbox=(bbox.west, bbox.south, bbox.east, bbox.north),
            hotspots=hotspots,
            rows_rejected=rejected,
            provenance=provenance,
        )
