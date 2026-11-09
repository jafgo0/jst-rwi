"""Emit the pinned product OpenAPI schema to docs/product/openapi.v0.json.

Run via ``make product-schema``. The committed pin is what
``tests/unit/test_product_schema_pin.py`` compares against, so an
intentional change to the product surface is a conscious re-export and a
visible diff in review.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rwi.api.app import create_app
from rwi.api.product import dumps_product_schema

PIN_PATH = Path(__file__).resolve().parents[1] / "docs" / "product" / "openapi.v0.json"


def main() -> int:
    pin = dumps_product_schema(create_app())
    PIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PIN_PATH.write_text(pin, encoding="utf-8")
    print(f"wrote {PIN_PATH.relative_to(PIN_PATH.parents[2])} ({len(pin)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
