"""Lazy feature-module loading with an honest "not available" answer.

The platform is built in increments. Rather than crashing at import time or -
far worse - inventing a placeholder answer, an endpoint whose module is not
present yet returns HTTP 501 naming the module and the capability.
"""

from __future__ import annotations

import importlib
from functools import lru_cache
from types import ModuleType
from typing import Any

from fastapi import HTTPException, status

_CAPABILITY_MODULES: dict[str, tuple[str, ...]] = {
    "solar": ("rwi.solar.geometry", "rwi.solar.clearsky"),
    "providers": ("rwi.providers.registry",),
    "quality": ("rwi.quality",),
    "route": ("rwi.route.loader", "rwi.route.geometry", "rwi.route.trajectory"),
    "energy": ("rwi.energy.poa", "rwi.energy.array", "rwi.energy.series"),
    "vehicle": ("rwi.vehicle.dynamics",),
    "strategy": ("rwi.strategy.scenarios",),
    "archive": ("rwi.archive",),
    "verification": ("rwi.verification",),
    "ensemble": ("rwi.ensemble",),
    "fusion": ("rwi.fusion",),
    "capture": ("rwi.capture",),
}


_IMPORT_ERRORS: dict[str, str] = {}


@lru_cache(maxsize=64)
def _try_import(module_name: str) -> ModuleType | None:
    """Import a feature module, recording why it failed rather than crashing.

    Any exception counts as "unavailable": a half-written module raises
    SyntaxError, a missing dependency raises ImportError, and a module that
    fails at import time raises whatever it likes. The platform must keep
    serving what still works and say plainly what it cannot do.
    """
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        _IMPORT_ERRORS[module_name] = f"{type(exc).__name__}: {exc}"
        return None


def import_errors() -> dict[str, str]:
    """Why each unavailable module could not be imported."""
    return dict(_IMPORT_ERRORS)


def capability_status() -> dict[str, bool]:
    """Which feature areas are actually importable right now."""
    return {
        name: all(_try_import(module) is not None for module in modules)
        for name, modules in _CAPABILITY_MODULES.items()
    }


def require(module_name: str, *, capability: str) -> ModuleType:
    """Import a feature module or raise 501 explaining what is missing."""
    module = _try_import(module_name)
    if module is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "error": {
                    "code": "CapabilityNotImplemented",
                    "message": f"{capability} is not available in this build",
                    "detail": _IMPORT_ERRORS.get(
                        module_name, f"module {module_name} could not be imported"
                    ),
                }
            },
        )
    return module


def require_attr(module_name: str, attribute: str, *, capability: str) -> Any:
    """Fetch one function from a feature module, or raise 501."""
    module = require(module_name, capability=capability)
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "error": {
                    "code": "CapabilityNotImplemented",
                    "message": f"{capability} is incomplete in this build",
                    "detail": f"{module_name}.{attribute} is missing",
                }
            },
        ) from exc
