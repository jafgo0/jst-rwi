"""Typed error hierarchy.

Errors are explicit so that the API layer can distinguish "we do not know" from
"something broke". The platform must never substitute a guess for missing data.
"""

from __future__ import annotations


class RwiError(Exception):
    """Base class for all platform errors."""


class NaiveDatetimeError(RwiError):
    """A datetime without tzinfo reached code that requires an aware UTC datetime."""


class InvalidCoordinateError(RwiError):
    """Latitude/longitude/elevation outside physically valid bounds."""


class UnknownVariableError(RwiError):
    """A variable name is not in the canonical variable registry."""


class UnitMismatchError(RwiError):
    """A value was supplied in a unit other than the canonical unit."""


class PositionUnavailableError(RwiError):
    """No usable position is available for the requested mode.

    Raised instead of falling back to a hardcoded location. The UI must show
    "position unknown", never an invented coordinate.
    """


class StaleDataError(RwiError):
    """Data exists but is older than the configured freshness limit."""


class ProviderError(RwiError):
    """Generic external weather provider failure."""


class ProviderUnavailableError(ProviderError):
    """Provider unreachable, timed out, or returned an unusable response."""


class ProviderRateLimitError(ProviderError):
    """Provider refused the request because of rate limiting or quota."""


class CapabilityNotSupportedError(ProviderError):
    """The provider does not offer the requested capability (e.g. observations)."""


class RouteError(RwiError):
    """Route file invalid, or a distance outside the route extent was requested."""


class ModelConfigurationError(RwiError):
    """A physical model was asked to run with missing or implausible parameters."""
