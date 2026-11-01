"""Provider registry and the fallback policy.

Fallback policy (also documented in docs/science/providers.md):

1. Providers are tried in the priority order they are given (dict/sequence order).
2. A provider is *skipped without a request* when its declared capabilities do
   not cover the request: wrong data kind, a requested variable it does not
   offer, or a forecast horizon beyond ``max_forecast_hours``.
3. A provider counts as failed when it raises a `ProviderError`, returns a series
   whose ``kind`` differs from the requested kind, or returns a series with no
   usable value for any requested variable.
4. The first usable series wins. It is returned unchanged except that its
   ``Measured.provenance.notes`` gain a line naming every provider that was
   skipped or failed before it and why (only when there was at least one).
5. The ``kind`` is never changed: asking for OBSERVATION can never return a
   FORECAST or ANALYSIS series.
6. If nothing succeeds: `ProviderUnavailableError` listing every attempt; or
   `CapabilityNotSupportedError` when *no* provider was even eligible (nothing
   was tried - e.g. observations requested but only model APIs are configured).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rwi.config import Settings, get_settings
from rwi.contracts import ProviderCapabilities, WeatherProvider, WeatherSeries, WeatherStep
from rwi.core.errors import (
    CapabilityNotSupportedError,
    ProviderError,
    ProviderUnavailableError,
)
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured
from rwi.core.timebase import TimeWindow, utc_now
from rwi.providers.bom import BomProvider
from rwi.providers.openmeteo import OpenMeteoProvider

__all__ = [
    "FallbackAttempt",
    "available_providers",
    "close_providers",
    "fetch_with_fallback",
    "fetch_with_fallback_detailed",
]

log = logging.getLogger("rwi.providers")

SUPPORTED_KINDS = (DataKind.FORECAST, DataKind.OBSERVATION, DataKind.ANALYSIS)


class FallbackAttempt(BaseModel):
    """One provider's part in a fallback decision."""

    model_config = ConfigDict(frozen=True)

    provider: str
    outcome: Literal["skipped", "failed", "used"]
    reason: str | None = None

    def describe(self) -> str:
        return f"{self.provider}: {self.outcome}" + (f" ({self.reason})" if self.reason else "")


def available_providers(settings: Settings | None = None) -> dict[str, WeatherProvider]:
    """Providers in default priority order, keyed by provider name.

    Order matters for `fetch_with_fallback`. Forecast: Open-Meteo is the only
    forecast source today. Observations: BoM is the only observation source;
    Open-Meteo does not offer observations and will be skipped for that kind.
    """
    s = settings or get_settings()
    providers: list[WeatherProvider] = [OpenMeteoProvider(s), BomProvider(s)]
    return {p.name: p for p in providers}


async def close_providers(providers: Mapping[str, WeatherProvider]) -> None:
    """Close the HTTP clients of providers that own one (call on application shutdown)."""
    for provider in providers.values():
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()


def _ordered(
    providers: Mapping[str, WeatherProvider] | Sequence[WeatherProvider],
) -> list[WeatherProvider]:
    if isinstance(providers, Mapping):
        return list(providers.values())
    return list(providers)


def _ineligible_reason(
    caps: ProviderCapabilities,
    window: TimeWindow,
    variables: Sequence[str],
    kind: DataKind,
    now: datetime,
) -> str | None:
    """Why ``caps`` cannot serve the request, or None if it can (as far as declared)."""
    if kind is DataKind.OBSERVATION and not caps.supports_observations:
        return "does not provide observations"
    if kind is DataKind.FORECAST and not caps.supports_forecast:
        return "does not provide forecasts"
    if kind is DataKind.ANALYSIS and not caps.supports_archive:
        return "does not provide analysis/archive data"
    missing = [v for v in variables if v not in caps.variables]
    if missing:
        return f"does not offer variable(s): {', '.join(missing)}"
    if kind is DataKind.FORECAST and caps.max_forecast_hours is not None:
        horizon = (window.end_utc - now).total_seconds() / 3600.0
        if horizon > caps.max_forecast_hours + 24.0:  # +24 h: horizon counted from 00:00 UTC
            return f"forecast horizon {horizon:.0f} h exceeds {caps.max_forecast_hours:g} h"
    return None


def _has_usable_value(series: WeatherSeries, variables: Sequence[str]) -> bool:
    return any(step.value(v) is not None for step in series.steps for v in variables)


def _annotate(series: WeatherSeries, attempts: Sequence[FallbackAttempt]) -> WeatherSeries:
    """Append the fallback trail to every value's provenance notes (copy; originals intact)."""
    trail = [a for a in attempts if a.outcome != "used"]
    if not trail:
        return series
    note = "Fallback: " + "; ".join(a.describe() for a in trail) + "."
    cache: dict[int, object] = {}
    new_steps: list[WeatherStep] = []
    for step in series.steps:
        values: dict[str, Measured] = {}
        for name, m in step.values.items():
            prov = cache.get(id(m.provenance))
            if prov is None:
                joined = f"{m.provenance.notes} {note}" if m.provenance.notes else note
                prov = m.provenance.model_copy(update={"notes": joined})
                cache[id(m.provenance)] = prov
            values[name] = m.model_copy(update={"provenance": prov})
        new_steps.append(WeatherStep(valid_time_utc=step.valid_time_utc, values=values))
    return series.model_copy(update={"steps": new_steps})


async def fetch_with_fallback_detailed(
    providers: Mapping[str, WeatherProvider] | Sequence[WeatherProvider],
    point: GeoPoint,
    window: TimeWindow,
    variables: Sequence[str],
    *,
    kind: DataKind,
    now: datetime | None = None,
) -> tuple[WeatherSeries, list[FallbackAttempt]]:
    """Like `fetch_with_fallback` but also returns the structured attempt log."""
    if kind not in SUPPORTED_KINDS:
        raise CapabilityNotSupportedError(
            f"fetch_with_fallback supports {[k.value for k in SUPPORTED_KINDS]}, not {kind.value}"
        )
    variables = list(dict.fromkeys(variables))
    now = now or utc_now()
    attempts: list[FallbackAttempt] = []
    tried = 0
    for provider in _ordered(providers):
        name = provider.name
        reason = _ineligible_reason(provider.capabilities, window, variables, kind, now)
        if reason:
            attempts.append(FallbackAttempt(provider=name, outcome="skipped", reason=reason))
            continue
        method = {
            DataKind.FORECAST: "fetch_forecast",
            DataKind.OBSERVATION: "fetch_observations",
            DataKind.ANALYSIS: "fetch_analysis",
        }[kind]
        fn = getattr(provider, method, None)
        if fn is None:
            attempts.append(
                FallbackAttempt(provider=name, outcome="skipped", reason=f"has no {method}")
            )
            continue
        tried += 1
        try:
            series: WeatherSeries = await fn(point, window, variables)
        except ProviderError as exc:
            log.warning("fallback: %s failed: %s", name, exc)
            attempts.append(FallbackAttempt(provider=name, outcome="failed", reason=str(exc)))
            continue
        if series.kind is not kind:
            attempts.append(
                FallbackAttempt(
                    provider=name,
                    outcome="failed",
                    reason=f"returned kind {series.kind.value}, requested {kind.value}; rejected",
                )
            )
            continue
        if not _has_usable_value(series, variables):
            attempts.append(
                FallbackAttempt(
                    provider=name,
                    outcome="failed",
                    reason="no usable value for any requested variable (all missing/flagged)",
                )
            )
            continue
        attempts.append(FallbackAttempt(provider=name, outcome="used"))
        return _annotate(series, attempts), attempts

    listing = "; ".join(a.describe() for a in attempts) or "no providers configured"
    if tried == 0:
        raise CapabilityNotSupportedError(
            f"no provider can serve {kind.value} for {list(variables)}: {listing}"
        )
    raise ProviderUnavailableError(
        f"all providers failed for {kind.value} {list(variables)}: {listing}"
    )


async def fetch_with_fallback(
    providers: Mapping[str, WeatherProvider] | Sequence[WeatherProvider],
    point: GeoPoint,
    window: TimeWindow,
    variables: Sequence[str],
    *,
    kind: DataKind,
) -> WeatherSeries:
    """First usable series of the requested ``kind`` in priority order (see module doc)."""
    series, _ = await fetch_with_fallback_detailed(providers, point, window, variables, kind=kind)
    return series
