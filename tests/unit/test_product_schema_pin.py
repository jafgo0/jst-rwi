"""The product-schema pin: changes to the STABLE surface must be visible.

The strategy repo consumes jst-rwi over HTTP. The pinned schema at
``docs/product/openapi.v0.json`` is the frozen description of that
surface. If an edit changes it, this test fails and asks for a
conscious re-export with ``make product-schema``. That is how an
accidental rename, a dropped field, a changed unit or a slipped tier
becomes impossible to miss.

The pin excludes INTERNAL and EXPERIMENTAL endpoints, and both sides
are normalised (sorted keys, volatile fields dropped) so a FastAPI
upgrade that reshuffles description strings does not trip the check.
"""

from __future__ import annotations

import json
from pathlib import Path

from rwi.api.app import create_app
from rwi.api.product import ProductTier, dumps_product_schema, tier_for_path

PIN_PATH = Path(__file__).resolve().parents[2] / "docs" / "product" / "openapi.v0.json"


def test_product_schema_matches_pin() -> None:
    current = dumps_product_schema(create_app())
    pinned = PIN_PATH.read_text(encoding="utf-8")
    assert current == pinned, (
        "The STABLE product surface changed. If this was intentional:\n"
        "  1. Bump jst-rwi version in pyproject.toml per docs/PRODUCT_CONTRACT.md.\n"
        "  2. Run: make product-schema\n"
        "  3. Commit the updated docs/product/openapi.v0.json.\n"
        "If this was NOT intentional, revert the change that moved a route\n"
        "into the STABLE tier or altered its response shape."
    )


def test_internal_prefixes_are_not_published() -> None:
    assert tier_for_path("/api/validation/ghi-bias") is ProductTier.INTERNAL
    assert tier_for_path("/api/satellite/satcorps-crosscheck") is ProductTier.INTERNAL
    assert tier_for_path("/health") is ProductTier.INTERNAL


def test_experimental_strategy_endpoints_are_not_in_stable_pin() -> None:
    assert tier_for_path("/api/strategy/scenarios") is ProductTier.EXPERIMENTAL
    assert tier_for_path("/api/strategy/vehicle") is ProductTier.EXPERIMENTAL
    pinned = PIN_PATH.read_text(encoding="utf-8")
    assert "/api/strategy/" not in pinned


def test_unknown_prefix_defaults_to_internal() -> None:
    """A new endpoint without an explicit tier never leaks into the pin."""
    assert tier_for_path("/api/brand-new-thing") is ProductTier.INTERNAL


def test_stable_prefixes_are_in_the_pin() -> None:
    pinned = PIN_PATH.read_text(encoding="utf-8")
    for prefix in (
        "/api/meteorologist/",
        "/api/state",
        "/api/route/",
        "/api/forecast/",
        "/api/solar/",
    ):
        assert prefix in pinned, f"STABLE prefix {prefix} missing from pinned schema"


def test_internal_only_models_stay_out_of_the_pin() -> None:
    """Models used only by INTERNAL routes (e.g. /api/map) must not look like STABLE changes."""
    schemas = json.loads(PIN_PATH.read_text(encoding="utf-8"))["components"]["schemas"]
    for name in ("GridProvenance", "SatelliteStatus", "MlBands"):
        assert name not in schemas
