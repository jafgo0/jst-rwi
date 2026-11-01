"""Shared plumbing for weather providers: HTTP, retry, rate limits, disk cache.

`BaseProvider` implements the transport half of the `WeatherProvider` protocol
(`rwi.contracts`). Concrete adapters (Open-Meteo, BoM) subclass it and add the
endpoint-specific request building and response parsing.

Behavioural guarantees (these are the point of the module):

* **Retry** - timeouts, transport errors, HTTP 429 and HTTP 5xx are retried up
  to ``settings.http_retries`` times with exponential backoff starting at
  ``settings.http_backoff_s``. A ``Retry-After`` header is honoured but capped
  at ``max_retry_after_s``; when the server asks for a longer wait (typical for
  an exhausted *daily* quota) we stop immediately instead of sleeping.
* **Typed failure** - a persistent 429 raises `ProviderRateLimitError`; every
  other persistent failure raises `ProviderUnavailableError`. A 4xx that is not
  a rate limit is a request problem and is not retried.
* **Disk cache** - raw JSON payloads are cached under
  ``settings.cache_dir/<provider>/`` keyed by (provider, dataset, point rounded
  to 0.01 deg, window, variables, extra request parameters). The cached entry
  stores the *original* ``retrieved_at_utc``; a cache hit never refreshes it, so
  data age is always honest. TTLs come from ``settings.cache_ttl_*``.
* **Offline mode** - with ``settings.allow_network == False`` the provider
  serves the cache (even past its TTL, because age stays honest and the quality
  layer flags it STALE) or raises `ProviderUnavailableError`. It never
  fabricates numbers and never degrades to a different data kind.
* **No silent stale fallback** - on network failure a stale cache entry is only
  served if the provider was constructed with ``serve_stale_on_error=True``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self

import httpx

from rwi.config import Settings, get_settings
from rwi.contracts import (
    EnsembleForecast,
    ProviderCapabilities,
    ProviderHealth,
    WeatherSeries,
)
from rwi.core.errors import (
    CapabilityNotSupportedError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, QualityFlag
from rwi.core.timebase import TimeWindow, ensure_utc, utc_now
from rwi.core.variables import spec_for

__all__ = [
    "USER_AGENT",
    "BaseProvider",
    "CacheEntry",
    "FetchResult",
    "ResponseCache",
    "ceil_hour",
    "classify_value",
    "floor_hour",
    "make_cache_key",
]

log = logging.getLogger("rwi.providers")

USER_AGENT = "jst-rwi/0.1 (JU Solar Team BWSC 2027 weather decision support; research use)"

SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], datetime]


# ---------------------------------------------------------------------------
# Small time helpers shared by adapters
# ---------------------------------------------------------------------------


def floor_hour(when: datetime) -> datetime:
    """Largest whole UTC hour <= ``when``."""
    return ensure_utc(when).replace(minute=0, second=0, microsecond=0)


def ceil_hour(when: datetime) -> datetime:
    """Smallest whole UTC hour >= ``when``."""
    w = ensure_utc(when)
    f = floor_hour(w)
    return f if f == w else f + timedelta(hours=1)


# ---------------------------------------------------------------------------
# Value classification at the provider boundary
# ---------------------------------------------------------------------------


def classify_value(variable: str, value: float | None) -> tuple[float | None, QualityFlag]:
    """Apply the registry's plausible range to a converted value.

    Returns ``(value, flag)``. ``None``/NaN/inf become ``(None, MISSING)``; a
    finite value outside the registry range is *kept* (so it can be inspected)
    but flagged ``OUT_OF_RANGE``, which makes it non-usable downstream. Values
    are never clipped or corrected.
    """
    if value is None or isinstance(value, bool) or not math.isfinite(float(value)):
        return None, QualityFlag.MISSING
    spec = spec_for(variable)
    v = float(value)
    if v < spec.minimum or v > spec.maximum:
        return v, QualityFlag.OUT_OF_RANGE
    return v, QualityFlag.OK


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


def make_cache_key(
    *,
    provider: str,
    dataset: str,
    point: GeoPoint,
    window: TimeWindow,
    variables: Sequence[str],
    extra: Mapping[str, Any] | None = None,
    precision: int = 2,
) -> dict[str, Any]:
    """Cache key for one request.

    The point is rounded to ``precision`` decimals (0.01 deg ~ 1.1 km, well below
    the 9-25 km grid of every supported model); the window is normalised to
    ISO-8601 UTC; variables are sorted. ``extra`` carries model names and other
    request parameters that change the answer.
    """
    return {
        "provider": provider,
        "dataset": dataset,
        "lat": round(point.latitude_deg, precision),
        "lon": round(point.longitude_deg, precision),
        "start_utc": ensure_utc(window.start_utc).isoformat(),
        "end_utc": ensure_utc(window.end_utc).isoformat(),
        "variables": sorted(set(variables)),
        "extra": dict(sorted((extra or {}).items())),
    }


@dataclass(frozen=True)
class CacheEntry:
    payload: Any
    retrieved_at_utc: datetime

    def age_s(self, now: datetime) -> float:
        return (ensure_utc(now) - self.retrieved_at_utc).total_seconds()


class ResponseCache:
    """JSON-file cache; one file per request key, written atomically."""

    SCHEMA = 1

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @staticmethod
    def digest(key: Mapping[str, Any]) -> str:
        blob = json.dumps(key, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:40]

    def path_for(self, key: Mapping[str, Any]) -> Path:
        provider = str(key.get("provider", "unknown"))
        return self.root / provider / f"{self.digest(key)}.json"

    def load(self, key: Mapping[str, Any]) -> CacheEntry | None:
        path = self.path_for(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("schema") != self.SCHEMA or raw.get("key") != json.loads(
                json.dumps(key, default=str)
            ):
                return None
            retrieved = ensure_utc(datetime.fromisoformat(raw["retrieved_at_utc"]))
            return CacheEntry(payload=raw["payload"], retrieved_at_utc=retrieved)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("ignoring unreadable cache file %s: %s", path, exc)
            return None

    def store(self, key: Mapping[str, Any], payload: Any, retrieved_at_utc: datetime) -> None:
        path = self.path_for(key)
        doc = {
            "schema": self.SCHEMA,
            "key": json.loads(json.dumps(key, default=str)),
            "retrieved_at_utc": ensure_utc(retrieved_at_utc).isoformat(),
            "payload": payload,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except OSError as exc:  # a cache that cannot be written must not break a fetch
            log.warning("could not write cache file %s: %s", path, exc)


@dataclass(frozen=True)
class FetchResult:
    """A raw payload plus the facts needed to describe it honestly."""

    payload: Any
    retrieved_at_utc: datetime
    from_cache: bool
    age_s: float
    """Seconds between ``retrieved_at_utc`` and the moment of this call."""


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------


class BaseProvider:
    """Transport, retry, cache and capability plumbing for weather adapters."""

    name: str = "base"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: SleepFn | None = None,
        clock: ClockFn | None = None,
        serve_stale_on_error: bool = False,
        max_retry_after_s: float = 60.0,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._now: ClockFn = clock or utc_now
        self.serve_stale_on_error = serve_stale_on_error
        self.max_retry_after_s = max_retry_after_s
        self._cache = ResponseCache(self.settings.cache_dir)

    # -- protocol surface ---------------------------------------------------

    @property
    def capabilities(self) -> ProviderCapabilities:  # pragma: no cover - abstract
        raise NotImplementedError

    async def fetch_forecast(
        self, point: GeoPoint, window: TimeWindow, variables: Sequence[str]
    ) -> WeatherSeries:
        raise CapabilityNotSupportedError(f"{self.name}: forecasts are not supported")

    async def fetch_observations(
        self, point: GeoPoint, window: TimeWindow, variables: Sequence[str]
    ) -> WeatherSeries:
        raise CapabilityNotSupportedError(f"{self.name}: observations are not supported")

    async def fetch_ensemble(
        self, point: GeoPoint, window: TimeWindow, variables: Sequence[str]
    ) -> EnsembleForecast:
        raise CapabilityNotSupportedError(f"{self.name}: ensemble forecasts are not supported")

    async def fetch_analysis(
        self, point: GeoPoint, window: TimeWindow, variables: Sequence[str]
    ) -> WeatherSeries:
        raise CapabilityNotSupportedError(
            f"{self.name}: analysis/reanalysis (archive) data is not supported"
        )

    async def health(self) -> ProviderHealth:
        checked = self._now()
        if not self.settings.allow_network:
            return ProviderHealth(
                provider=self.name,
                reachable=False,
                checked_at_utc=checked,
                message="network disabled by configuration (RWI_ALLOW_NETWORK=false)",
            )
        t0 = time.perf_counter()
        try:
            await self._probe()
        except ProviderError as exc:
            return ProviderHealth(
                provider=self.name, reachable=False, checked_at_utc=checked, message=str(exc)
            )
        return ProviderHealth(
            provider=self.name,
            reachable=True,
            checked_at_utc=checked,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _probe(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- lifecycle ----------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.http_timeout_s,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                follow_redirects=True,
            )
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- capability checks --------------------------------------------------

    def _check_variables(
        self,
        variables: Sequence[str],
        supported: frozenset[str] | None = None,
    ) -> list[str]:
        """Validate and de-duplicate requested variables (order preserved).

        Unknown names raise `UnknownVariableError` (a caller bug); registry names
        this provider cannot deliver raise `CapabilityNotSupportedError`.
        """
        if not variables:
            raise CapabilityNotSupportedError(f"{self.name}: no variables requested")
        out: list[str] = []
        for v in variables:
            spec_for(v)
            if v not in out:
                out.append(v)
        allowed = supported if supported is not None else self.capabilities.variables
        missing = [v for v in out if v not in allowed]
        if missing:
            raise CapabilityNotSupportedError(
                f"{self.name}: variables not supported by this dataset: {', '.join(missing)}"
            )
        return out

    def _ttl_s(self, kind: DataKind) -> float:
        if kind is DataKind.OBSERVATION:
            return self.settings.cache_ttl_observation_s
        return self.settings.cache_ttl_forecast_s

    # -- cached fetch ---------------------------------------------------------

    async def _cached(
        self,
        key: Mapping[str, Any],
        ttl_s: float,
        fetch: Callable[[], Awaitable[Any]],
    ) -> FetchResult:
        """Serve a fresh cache entry, else fetch, else (offline) the stale cache, else raise."""
        now = self._now()
        entry = self._cache.load(key)
        if entry is not None and ttl_s > 0 and entry.age_s(now) <= ttl_s:
            return FetchResult(entry.payload, entry.retrieved_at_utc, True, entry.age_s(now))

        if not self.settings.allow_network:
            if entry is not None:
                log.info("%s: offline, serving cache aged %.0f s", self.name, entry.age_s(now))
                return FetchResult(entry.payload, entry.retrieved_at_utc, True, entry.age_s(now))
            raise ProviderUnavailableError(
                f"{self.name}: network access is disabled (allow_network=False) and there is "
                "no cached data for this request; refusing to invent values"
            )

        try:
            payload = await fetch()
        except ProviderError:
            if entry is not None and self.serve_stale_on_error:
                log.warning(
                    "%s: fetch failed, serving stale cache aged %.0f s", self.name, entry.age_s(now)
                )
                return FetchResult(entry.payload, entry.retrieved_at_utc, True, entry.age_s(now))
            raise
        retrieved = self._now()
        self._cache.store(key, payload, retrieved)
        return FetchResult(payload, retrieved, False, 0.0)

    # -- HTTP with retry ------------------------------------------------------

    @staticmethod
    def _reason(response: httpx.Response) -> str:
        try:
            body = response.json()
            if isinstance(body, dict) and "reason" in body:
                return str(body["reason"])[:300]
        except ValueError:
            pass
        return response.text[:200].strip() or response.reason_phrase

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code in (400, 403, 503):
            text = response.text[:500].lower()
            return any(m in text for m in ("limit exceeded", "too many requests", "rate limit"))
        return False

    @staticmethod
    def _retry_after_s(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None  # HTTP-date form is not used by the supported providers

    async def _get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET ``url`` and return decoded JSON, with retry/backoff and typed errors.

        Error messages never contain the query string (it may carry an API key).
        """
        attempts = max(0, int(self.settings.http_retries)) + 1
        failure: ProviderError = ProviderUnavailableError(f"{self.name}: no attempt made")
        for attempt in range(attempts):
            wait_hint: float | None = None
            try:
                response = await self._http().get(url, params=dict(params or {}))
            except httpx.TimeoutException:
                failure = ProviderUnavailableError(
                    f"{self.name}: timeout after {self.settings.http_timeout_s:g} s calling {url}"
                )
            except httpx.TransportError as exc:
                failure = ProviderUnavailableError(
                    f"{self.name}: network error calling {url}: {type(exc).__name__}"
                )
            else:
                if self._is_rate_limited(response):
                    wait_hint = self._retry_after_s(response)
                    failure = ProviderRateLimitError(
                        f"{self.name}: rate limited (HTTP {response.status_code}) by {url}: "
                        f"{self._reason(response)}"
                        + (f" [Retry-After {wait_hint:g} s]" if wait_hint is not None else "")
                    )
                    if wait_hint is not None and wait_hint > self.max_retry_after_s:
                        raise failure  # e.g. exhausted daily quota: waiting is pointless
                elif response.status_code >= 500:
                    wait_hint = self._retry_after_s(response)
                    failure = ProviderUnavailableError(
                        f"{self.name}: HTTP {response.status_code} from {url}: "
                        f"{self._reason(response)}"
                    )
                elif response.status_code != 200:
                    raise ProviderUnavailableError(
                        f"{self.name}: request rejected, HTTP {response.status_code} from "
                        f"{url}: {self._reason(response)}"
                    )
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ProviderUnavailableError(
                            f"{self.name}: {url} returned HTTP 200 with a body that is not JSON"
                        ) from exc
            if attempt < attempts - 1:
                delay = self.settings.http_backoff_s * (2**attempt)
                if wait_hint is not None:
                    delay = max(delay, min(wait_hint, self.max_retry_after_s))
                log.info(
                    "%s: attempt %d/%d failed (%s); retrying in %.2f s",
                    self.name,
                    attempt + 1,
                    attempts,
                    failure,
                    delay,
                )
                await self._sleep(delay)
        raise failure
