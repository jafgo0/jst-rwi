"""Load JU Solar Team's shadow-analysis efficiency matrix.

The team's ``efficiency_matrix.mat`` is a (180, 45, N) array of ABSOLUTE
cell efficiency: how much of unit GHI a given MPPT group converts to DC
power at (azimuth, zenith). It is the output of their CAD-based ray-tracer
that accounts for per-cell orientation on the cambered body.

Our :class:`~rwi.energy.angular.AngularResponseTable` contract expects values
in ``[0, 1]`` as a MULTIPLIER on the modelled flat-plane output. To bridge
the two conventions without changing the contract or wiring in a second
model, the loader normalises by the array's own ``cell_efficiency_stc``:
``values = raw / stc_efficiency``. A cell that performs at nameplate at sun
noon then reads 1.0; a cell under 50 % AOI loss reads 0.5. The semantics line
up with the identity default and the existing tests, and the one line of
provenance records what was done.

The alternative - treating the matrix as absolute - would require a second
code path in :func:`rwi.energy.array.array_power` that bypasses the nameplate
efficiency. One code path, one documented transformation, is strictly simpler
than two code paths doing almost the same thing differently.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from rwi.contracts import ArrayConfig
from rwi.core.errors import ModelConfigurationError
from rwi.energy.angular import (
    AZIMUTH_BINS,
    ZENITH_BINS,
    AngularResponseTable,
)

log = logging.getLogger(__name__)


def load_team_efficiency_matrix(
    path: Path,
    *,
    stc_efficiency: float,
    name: str = "ju-solar-team-shadow-analysis",
) -> AngularResponseTable:
    """Load the team's efficiency_matrix.mat and normalise to our convention.

    Args:
        path: Path to the ``.mat`` file produced by the team's
            ``shadow-analysis/panel_table.py``.
        stc_efficiency: Cell efficiency at Standard Test Conditions, dimensionless
            (e.g. 0.27 for Oxford PV perovskite-silicon). The team's matrix
            values are divided by this so the result is a multiplier in
            ``[0, 1]`` on the modelled flat-plane output.
        name: Human-readable label stored on the returned table.

    Raises:
        ModelConfigurationError: If stc_efficiency is not strictly positive,
            or if the loaded matrix has values larger than stc_efficiency by
            more than a small tolerance (which would indicate the matrix is
            NOT absolute efficiency, in which case this transformation is
            wrong and the caller must adopt a different approach).
    """
    import scipy.io

    if stc_efficiency <= 0.0:
        raise ModelConfigurationError(
            f"stc_efficiency must be > 0 to normalise the matrix, got {stc_efficiency}"
        )

    contents = scipy.io.loadmat(path, squeeze_me=True)
    matrices = [
        value
        for key, value in contents.items()
        if not key.startswith("_") and isinstance(value, np.ndarray) and value.ndim == 3
    ]
    if len(matrices) != 1:
        raise ModelConfigurationError(
            f"{path.name}: expected one 3-D array, found {len(matrices)}"
        )
    raw = matrices[0].astype(np.float32)

    if raw.shape[:2] != (AZIMUTH_BINS, ZENITH_BINS):
        raise ModelConfigurationError(
            f"{path.name}: shape {raw.shape[:2]} does not match the "
            f"({AZIMUTH_BINS}, {ZENITH_BINS}) azimuth x zenith grid our contract uses"
        )
    cell_count = raw.shape[2]

    # Sanity check: the matrix should be absolute cell efficiency. If any
    # value exceeds stc_efficiency * 1.05, the matrix is not what we think
    # and normalising would silently rescale real numbers wrong.
    overshoot = float(raw.max()) / stc_efficiency
    if overshoot > 1.05:
        raise ModelConfigurationError(
            f"{path.name}: max value {raw.max():.4f} exceeds "
            f"stc_efficiency={stc_efficiency} by {(overshoot - 1) * 100:.1f} %. "
            "This matrix is NOT absolute cell efficiency relative to the given "
            "stc_efficiency - refusing to normalise rather than silently scale "
            "the physical interpretation. Check that stc_efficiency matches the "
            "array the matrix was generated for."
        )

    normalised = (raw / stc_efficiency).clip(0.0, 1.0)
    peak_before = float(raw.max())
    peak_after = float(normalised.max())
    log.info(
        "loaded %s: shape %s, peak %.4f (abs eff) -> %.4f (multiplier) "
        "using stc_efficiency=%.3f",
        path.name, raw.shape, peak_before, peak_after, stc_efficiency,
    )

    return AngularResponseTable(
        name=name,
        source="measured",
        cell_count=cell_count,
        values=normalised,
        notes=(
            f"Loaded from {path.name} (JU Solar Team shadow-analysis output). "
            f"Original matrix is absolute cell efficiency with peak "
            f"{peak_before:.4f}; normalised by stc_efficiency={stc_efficiency} "
            f"to multiplier in [0, 1] (peak {peak_after:.4f}) so it slots into "
            "ArrayConfig.angular_response without changing the energy model."
        ),
    )


def enrich_array_with_team_matrix(
    array: ArrayConfig,
    path: Path,
    *,
    name: str | None = None,
) -> ArrayConfig:
    """Return a new ArrayConfig whose angular_response is the team's matrix.

    This is the analogue of ``enrich_with_team_elevation`` for the array:
    one function call turns a nameplate-only ArrayConfig into one that
    describes the per-cell geometry the team has actually measured.
    """
    table = load_team_efficiency_matrix(
        path,
        stc_efficiency=array.cell_efficiency_stc,
        name=name or f"team-shadow-{path.stem}",
    )
    return array.model_copy(update={"angular_response": table})
