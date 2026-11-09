"""Product-contract machinery: tiers, version header, OpenAPI pin.

The strategy repo consumes jst-rwi over HTTP. This module defines the
stable surface it is allowed to depend on and the pin-file that detects
accidental breakage.

Tiers (see docs/PRODUCT_CONTRACT.md):

* ``STABLE``   - stable FORM, versioned; the strategy repo may depend on
                 the shape of these responses. STABLE does NOT promise
                 that every number is validated; it promises that fields,
                 units and provenance structure do not change without a
                 major version bump.
* ``EXPERIMENTAL`` - implemented, not validated. The strategy repo may
                     read them for development but must not build final
                     decisions on their numbers.
* ``INTERNAL`` - operator/UI endpoints. Not part of the product; may
                 change without notice and are stripped from the pinned
                 product schema.

The tier is a prefix-level rule here so a new router under an existing
prefix inherits the right tier automatically. New prefixes must be added
to :data:`TIER_BY_PREFIX` in the same PR (the CLAUDE.md rail checks it).
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Final

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


class ProductTier(StrEnum):
    STABLE = "stable"
    EXPERIMENTAL = "experimental"
    INTERNAL = "internal"


# Longest-prefix match wins; order matters only for readability.
TIER_BY_PREFIX: Final[dict[str, ProductTier]] = {
    # STABLE: form-stable product surface.
    "/api/meteorologist": ProductTier.STABLE,
    "/api/route": ProductTier.STABLE,
    "/api/solar": ProductTier.STABLE,
    "/api/forecast": ProductTier.STABLE,
    "/api/state": ProductTier.STABLE,
    "/api/weather": ProductTier.STABLE,
    "/api/alerts": ProductTier.STABLE,
    "/api/energy": ProductTier.STABLE,
    # EXPERIMENTAL: implemented, not validated; the strategy repo lives
    # in its own repository, so the strategy endpoints here are a
    # preview of the integration surface.
    "/api/strategy": ProductTier.EXPERIMENTAL,
    # INTERNAL: operator/UI/health. Not part of the product.
    "/api/validation": ProductTier.INTERNAL,
    "/api/satellite": ProductTier.INTERNAL,
    "/health": ProductTier.INTERNAL,
}


def tier_for_path(path: str) -> ProductTier:
    """Longest-prefix match; paths that match nothing are INTERNAL.

    The default is INTERNAL (not STABLE) on purpose: a new endpoint
    without an explicit tier never leaks into the pinned product schema.
    """
    matched = ""
    tier = ProductTier.INTERNAL
    for prefix, t in TIER_BY_PREFIX.items():
        if path.startswith(prefix) and len(prefix) > len(matched):
            matched = prefix
            tier = t
    return tier


class ProductVersionMiddleware(BaseHTTPMiddleware):
    """Set ``X-Rwi-Product-Version`` and ``X-Rwi-Product-Tier`` on every response.

    The version is the single source of truth from
    :mod:`importlib.metadata`; the tier tells the strategy client how
    much it may lean on the response shape.
    """

    def __init__(self, app: Any, *, version: str) -> None:
        super().__init__(app)
        self._version = version

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Rwi-Product-Version"] = self._version
        response.headers["X-Rwi-Product-Tier"] = tier_for_path(request.url.path).value
        return response


def product_schema(app: FastAPI) -> dict[str, Any]:
    """Build the pinned product schema: STABLE routes only, sorted & normalised.

    FastAPI upgrades can reorder keys, rename example fields, or shuffle
    ``components`` subtrees. Those are not product changes; the diff
    tolerates them by sorting every dict and dropping volatile fields
    before serialising. What remains - operation ids, path parameters,
    schema shapes, field names and types - IS the contract.
    """
    full = app.openapi()
    paths = full.get("paths", {})
    stable_paths = {
        p: entry
        for p, entry in paths.items()
        if tier_for_path(p) is ProductTier.STABLE
    }
    schema: dict[str, Any] = {
        "openapi_subset": "product-v1",  # not an openapi version - our shape tag
        "info": {
            "title": full.get("info", {}).get("title", "jst-rwi"),
            # version is checked separately through the X-Rwi-Product-Version
            # header; pinning it here would force a schema rewrite on every
            # patch release, which is noise.
        },
        "paths": stable_paths,
        "components": full.get("components", {}),
    }
    return _normalise(schema)


def _normalise(obj: Any) -> Any:
    """Recursively sort dict keys and strip fields that drift with FastAPI."""
    DROP = {"example", "examples", "description", "summary", "title"}
    if isinstance(obj, dict):
        return {
            k: _normalise(v)
            for k, v in sorted(obj.items())
            if k not in DROP
        }
    if isinstance(obj, list):
        return [_normalise(x) for x in obj]
    return obj


def dumps_product_schema(app: FastAPI) -> str:
    """Canonical JSON form for the pin file: sorted keys, trailing newline."""
    return json.dumps(product_schema(app), sort_keys=True, indent=2) + "\n"


__all__ = [
    "TIER_BY_PREFIX",
    "ProductTier",
    "ProductVersionMiddleware",
    "dumps_product_schema",
    "product_schema",
    "tier_for_path",
]
