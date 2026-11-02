"""Per-cell angular response: how each cell sees the sun as the car's heading
changes.

A flat-plane PV model treats the whole array as one horizontal surface. That is
fine for a stationary rooftop installation and wrong for a solar car, where
the vehicle's heading rotates the panel relative to the sun on every bend.
JU Solar Team's own ``shadow-analysis`` tool produces an efficiency matrix of
shape ``180 x 45 x N`` -- azimuth bins (2 deg) x zenith bins (2 deg) x
cell count -- that captures the per-cell orientation on the actual CAD body.

This module defines the type the matrix fills. Until the team's matrix is
loaded, the default is an identity lookup that always returns 1.0, which is
mathematically equivalent to the current flat-plane model and keeps every
existing test green. When the matrix lands, replacing the default is a one
line change in the ``ArrayConfig`` the strategy engine passes in, with no
callers to update.

The design priorities, in this order:

1. **Interface over implementation.** The shape and parameters match what
   ``shadow-analysis/panel_table.py:21`` already writes, so the team's own
   file loads without translation.
2. **Honest default.** The identity response is not a model of the real panel,
   and the type says so plainly: ``source="identity"`` and ``is_measured`` is
   False. A consumer that wants to know whether they are using real angular
   data must be able to tell.
3. **Fail noisily on shape mismatch.** A 338-cell matrix multiplied by a
   339-cell summation is a silent, catastrophic error. The validator rejects
   it at construction time.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from pathlib import Path

#: Azimuth bin size in degrees. 180 bins of 2 deg covers 0..360.
AZIMUTH_BIN_DEG: Final = 2.0
AZIMUTH_BINS: Final = 180
#: Zenith bin size in degrees. 45 bins of 2 deg covers 0..90 (horizon to zenith).
ZENITH_BIN_DEG: Final = 2.0
ZENITH_BINS: Final = 45


class AngularResponseTable(BaseModel):
    """How much of unit input irradiance each cell delivers, by sun angle.

    Values are dimensionless and in ``[0, 1]``. A value of 1.0 means the cell
    receives the full plane-of-array irradiance; 0.0 means it is in deep shadow
    or facing fully away. The product of the table and the mean cell count
    gives the array's effective fractional response at that angle.

    The matrix is relative to the ``vehicle frame``, not the world: azimuth 0
    is straight ahead of the car. Converting world sun azimuth to vehicle
    azimuth happens in the caller, so the table itself does not need to know
    the vehicle's current heading.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str = Field(description="short human label, e.g. 'tesseract-2025-flat'")
    source: Literal["identity", "measured", "simulated"] = Field(
        description="identity = flat-plane fallback (not a model of the real array); "
        "measured = team's shadow-analysis matrix from CAD + layout; "
        "simulated = analytical or synthetic data (clearly not reality)",
    )
    cell_count: int = Field(ge=1)
    values: np.ndarray = Field(
        description="shape (AZIMUTH_BINS, ZENITH_BINS, cell_count), dtype float32, in [0, 1]",
    )
    notes: str = ""

    def model_post_init(self, __context: object) -> None:
        if self.values.ndim != 3:
            raise ValueError(
                f"angular response matrix must be 3-D (azimuth, zenith, cells), "
                f"got shape {self.values.shape}"
            )
        if self.values.shape[:2] != (AZIMUTH_BINS, ZENITH_BINS):
            raise ValueError(
                f"angular response grid must be {AZIMUTH_BINS} azimuth x {ZENITH_BINS} "
                f"zenith bins, got {self.values.shape[:2]}"
            )
        if self.values.shape[2] != self.cell_count:
            raise ValueError(
                f"cell dimension {self.values.shape[2]} does not match cell_count="
                f"{self.cell_count}; refusing to silently sum the wrong panel"
            )
        if not np.all(np.isfinite(self.values)):
            raise ValueError("angular response matrix contains non-finite values")
        if float(self.values.min()) < 0.0 or float(self.values.max()) > 1.0:
            raise ValueError(
                f"angular response values must lie in [0, 1], got "
                f"[{self.values.min():.3f}, {self.values.max():.3f}]"
            )

    @property
    def is_measured(self) -> bool:
        """True iff this table came from measurements (CAD plus the real
        layout), not from the identity fallback or from a synthetic model."""
        return self.source == "measured"

    def mean_response(self, azimuth_deg: float, zenith_deg: float) -> float:
        """Mean cell response at the given sun angle, in the vehicle frame.

        Returns a single scalar in [0, 1] that multiplies the flat-plane POA
        irradiance in the array-power model. ``azimuth_deg`` wraps modulo 360;
        ``zenith_deg`` beyond 90 (sun below horizon) returns 0.0, matching the
        night-time invariant enforced everywhere else in the platform.
        """
        if zenith_deg >= 90.0 or zenith_deg < 0.0:
            return 0.0
        az_bin = int(azimuth_deg % 360.0 // AZIMUTH_BIN_DEG) % AZIMUTH_BINS
        zen_bin = min(ZENITH_BINS - 1, int(zenith_deg // ZENITH_BIN_DEG))
        slice_ = self.values[az_bin, zen_bin, :]
        return float(slice_.mean())


@lru_cache(maxsize=1)
def identity_response(cell_count: int = 1, *, name: str = "identity") -> AngularResponseTable:
    """The default angular response: 1.0 everywhere, for every cell.

    Mathematically equivalent to the current flat-plane model, so loading this
    into an ``ArrayConfig`` changes nothing until a real matrix replaces it.
    The point of having it as a distinct object is that a consumer that wants
    to KNOW whether the model has real per-cell geometry can check
    ``array.angular_response.is_measured``.
    """
    values = np.ones((AZIMUTH_BINS, ZENITH_BINS, cell_count), dtype=np.float32)
    return AngularResponseTable(
        name=name,
        source="identity",
        cell_count=cell_count,
        values=values,
        notes=(
            "Identity response, not measured data. Treats the array as a single "
            "flat horizontal plane - no per-cell orientation, no shadow of the "
            "vehicle body on itself. Replace with the team's shadow-analysis "
            "efficiency_matrix (drive-system shadow-analysis/panel_table.py) "
            "when it is available."
        ),
    )


def load_shadow_matrix(path: Path, *, name: str, cell_count: int) -> AngularResponseTable:
    """Load an efficiency matrix produced by the team's shadow-analysis tool.

    Expects a NumPy ``.npy`` or MATLAB ``.mat`` file with a single 3-D array
    of shape ``(180, 45, cell_count)``. The validator rejects anything else
    rather than silently using the wrong panel.

    The loader is intentionally simple: the shape, dtype and value range are
    the contract. If the team's writer changes format, the error will say so.
    """
    import scipy.io  # local to keep energy's cold import light

    suffix = path.suffix.lower()
    if suffix == ".npy":
        arr = np.load(path).astype(np.float32)
    elif suffix == ".mat":
        contents = scipy.io.loadmat(path, squeeze_me=True)
        matrices = [
            value
            for key, value in contents.items()
            if not key.startswith("_") and isinstance(value, np.ndarray) and value.ndim == 3
        ]
        if len(matrices) != 1:
            raise ValueError(
                f"{path.name}: expected exactly one 3-D array in the .mat file, "
                f"found {len(matrices)}"
            )
        arr = matrices[0].astype(np.float32)
    else:
        raise ValueError(
            f"{path.name}: unsupported file type {suffix!r}; use .npy or .mat"
        )
    return AngularResponseTable(
        name=name,
        source="measured",
        cell_count=cell_count,
        values=arr,
        notes=f"loaded from {path.name} ({suffix[1:].upper()})",
    )
