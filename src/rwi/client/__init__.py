"""Thin HTTP client for the jst-rwi product surface.

This is a CONVENIENCE for in-tree tests and for a strategy codebase that
lives in the same monorepo. The real contract is the pinned OpenAPI
schema at ``docs/product/openapi.v0.json`` and the version header on
every response - not an import of :mod:`rwi.contracts`.

When the strategy codebase is split into its own repo, the right move
is a slim client with its own minimal response models so the consumer
does not pull in pvlib, numpy, h5py, earthaccess and the rest of the
jst-rwi dependency closure. Importing :mod:`rwi` must never be a
load-bearing choice for the strategy.
"""

from __future__ import annotations

from rwi.client.client import RwiClient, RwiClientError

__all__ = ["RwiClient", "RwiClientError"]
