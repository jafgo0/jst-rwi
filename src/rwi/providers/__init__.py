"""Weather data providers: adapters, retry/cache plumbing and the fallback policy."""

from rwi.providers.base import BaseProvider
from rwi.providers.bom import BomProvider
from rwi.providers.openmeteo import OpenMeteoProvider
from rwi.providers.registry import (
    FallbackAttempt,
    available_providers,
    close_providers,
    fetch_with_fallback,
    fetch_with_fallback_detailed,
)

__all__ = [
    "BaseProvider",
    "BomProvider",
    "FallbackAttempt",
    "OpenMeteoProvider",
    "available_providers",
    "close_providers",
    "fetch_with_fallback",
    "fetch_with_fallback_detailed",
]
