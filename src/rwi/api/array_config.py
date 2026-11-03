"""Resolve the active ArrayConfig, enriched with the team's shadow matrix when
one has been configured.

This exists so the four places that used to call
``require_attr("rwi.energy.array", "DEFAULT_ARRAY", ...)`` go through one
helper that applies the configured enrichment. Without the helper, enabling
the matrix would need the same code copy-pasted in every endpoint; one of
them would get forgotten and the energy model would silently see one array
on one path and another on the next.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from rwi.api.capability import require_attr
from rwi.config import Settings, get_settings

log = logging.getLogger(__name__)


def get_active_array() -> Any:
    """Return DEFAULT_ARRAY, enriched with the team shadow matrix when
    ``RWI_TEAM_SHADOW_MATRIX`` is set and the file loads cleanly.

    An enrichment failure (missing file, wrong shape, mismatched STC) is
    logged and the un-enriched DEFAULT_ARRAY is returned - a bad path to the
    matrix must not stop the energy endpoints, but it must not silently
    claim per-cell geometry either.
    """
    return _resolve_array_cached(_matrix_key())


def _matrix_key() -> tuple[str | None, float, float]:
    """A cache key built from the inputs the result depends on.

    Changing the matrix path or array efficiency invalidates the cache; a
    fresh get_settings() does not, because its content is already cached.
    """
    settings = get_settings()
    default_array = require_attr("rwi.energy.array", "DEFAULT_ARRAY", capability="PV model")
    path_s = str(settings.team_shadow_matrix) if settings.team_shadow_matrix else None
    return (path_s, float(default_array.cell_efficiency_stc), float(default_array.area_m2))


@lru_cache(maxsize=4)
def _resolve_array_cached(_key: tuple[str | None, float, float]) -> Any:
    settings: Settings = get_settings()
    default_array = require_attr("rwi.energy.array", "DEFAULT_ARRAY", capability="PV model")
    if settings.team_shadow_matrix is None:
        return default_array
    try:
        enrich = require_attr(
            "rwi.energy.shadow",
            "enrich_array_with_team_matrix",
            capability="shadow-matrix enrichment",
        )
        enriched = enrich(default_array, settings.team_shadow_matrix)
        log.info(
            "ArrayConfig enriched with %s", settings.team_shadow_matrix.name,
        )
        return enriched
    except Exception as exc:
        log.warning(
            "could not enrich array with %s: %s - serving DEFAULT_ARRAY unchanged",
            settings.team_shadow_matrix, exc,
        )
        return default_array


__all__ = ["get_active_array"]
