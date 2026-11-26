"""Nowcast interface for 0-6 h irradiance (ADR 0008 §3) - skeleton.

`Nowcaster` is the seam a learned nowcaster will plug into. Today only the
physical baselines from `rwi.satellite.nowcast` implement it; `NeuralNowcaster`
refuses until a model exists and has beaten persistence on held-out scenes.
Outputs keep the satellite module's ``kind = DERIVED`` labelling: a nowcast is
never an observation (hard rule 1).
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from rwi.contracts import IrradianceNowcast, SatelliteScene
from rwi.core.geo import GeoPoint
from rwi.satellite.nowcast import ClearSky, nowcast_point, persistence_nowcast

__all__ = [
    "AdvectionNowcaster",
    "NeuralNowcaster",
    "Nowcaster",
    "PersistenceNowcaster",
    "extract_patch",
]

FloatArray = NDArray[np.float64]


@runtime_checkable
class Nowcaster(Protocol):
    """Anything that turns recent satellite scenes into a point irradiance nowcast."""

    name: str

    def nowcast(
        self,
        point: GeoPoint,
        scenes: Sequence[SatelliteScene],
        values: Sequence[FloatArray],
        *,
        horizon_s: float,
        step_s: float,
        clear_sky: ClearSky,
        issued_at_utc: datetime | None = None,
    ) -> IrradianceNowcast:
        """GHI nowcast at ``point`` (kind DERIVED, never OBSERVATION)."""
        ...


class PersistenceNowcaster:
    """Baseline: clear-sky index held constant (`persistence_nowcast`)."""

    name = "persistence"

    def nowcast(
        self,
        point: GeoPoint,
        scenes: Sequence[SatelliteScene],
        values: Sequence[FloatArray],
        *,
        horizon_s: float,
        step_s: float,
        clear_sky: ClearSky,
        issued_at_utc: datetime | None = None,
    ) -> IrradianceNowcast:
        """Persistence nowcast; provenance from `rwi.satellite.nowcast`."""
        return persistence_nowcast(
            point, scenes, values, horizon_s=horizon_s, step_s=step_s,
            clear_sky=clear_sky, issued_at_utc=issued_at_utc,
        )


class AdvectionNowcaster:
    """Cloud-motion advection (`nowcast_point`), falling back to persistence itself."""

    name = "advection"

    def nowcast(
        self,
        point: GeoPoint,
        scenes: Sequence[SatelliteScene],
        values: Sequence[FloatArray],
        *,
        horizon_s: float,
        step_s: float,
        clear_sky: ClearSky,
        issued_at_utc: datetime | None = None,
    ) -> IrradianceNowcast:
        """Advection nowcast; provenance from `rwi.satellite.nowcast`."""
        return nowcast_point(
            point, scenes, values, horizon_s=horizon_s, step_s=step_s,
            clear_sky=clear_sky, issued_at_utc=issued_at_utc,
        )


class NeuralNowcaster:
    """Placeholder for a learned nowcaster (ConvLSTM/U-Net on patches). Not trained."""

    name = "neural"

    def nowcast(
        self,
        point: GeoPoint,
        scenes: Sequence[SatelliteScene],
        values: Sequence[FloatArray],
        *,
        horizon_s: float,
        step_s: float,
        clear_sky: ClearSky,
        issued_at_utc: datetime | None = None,
    ) -> IrradianceNowcast:
        """Always raises: no learned nowcaster exists yet (ADR 0008 §3)."""
        raise NotImplementedError(
            "no learned nowcaster is trained; use PersistenceNowcaster or AdvectionNowcaster"
        )


def _nearest(coords: Sequence[float], value: float) -> int | None:
    arr = np.asarray(coords, dtype=float)
    if arr.size == 0:
        return None
    lo, hi = float(arr.min()), float(arr.max())
    if not lo <= value <= hi:
        return None
    return int(np.argmin(np.abs(arr - value)))


def extract_patch(
    scene: SatelliteScene, values: FloatArray, point: GeoPoint, half_size_px: int
) -> FloatArray | None:
    """Square ``(2h+1)^2`` pixel patch centred on ``point``; None if it leaves the grid."""
    if half_size_px < 0:
        raise ValueError("half_size_px must be >= 0")
    arr = np.asarray(values, dtype=float)
    if arr.shape != tuple(scene.grid.shape):
        raise ValueError(f"values shape {arr.shape} != grid shape {scene.grid.shape}")
    r = _nearest(scene.grid.latitudes, point.latitude_deg)
    c = _nearest(scene.grid.longitudes, point.longitude_deg)
    if r is None or c is None:
        return None
    h = half_size_px
    if r - h < 0 or c - h < 0 or r + h >= arr.shape[0] or c + h >= arr.shape[1]:
        return None
    return arr[r - h : r + h + 1, c - h : c + h + 1].copy()
