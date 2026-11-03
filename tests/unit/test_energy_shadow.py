"""Loading JU Solar Team's shadow-analysis efficiency matrix.

A matrix that scales the model wrong is worse than no matrix at all. These
tests pin the semantics: absolute efficiency in, multiplier in [0, 1] out,
with a loud refusal when the two conventions do not match.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import scipy.io

from rwi.core.errors import ModelConfigurationError
from rwi.energy.angular import AZIMUTH_BINS, ZENITH_BINS, AngularResponseTable
from rwi.energy.array import DEFAULT_ARRAY
from rwi.energy.shadow import enrich_array_with_team_matrix, load_team_efficiency_matrix


def _write_matrix(path: Path, values: np.ndarray) -> None:
    """Save a 3-D array in the same MATLAB format the team's exporter uses."""
    scipy.io.savemat(path, {"efficiency_matrix": values})


def test_loader_normalises_absolute_efficiency_to_a_multiplier(tmp_path: Path) -> None:
    """A cell operating at nameplate (0.27) at best orientation must read 1.0;
    a cell at 50% of nameplate must read 0.5. Nothing else."""
    stc = 0.27
    raw = np.full((AZIMUTH_BINS, ZENITH_BINS, 4), stc, dtype=np.float32)
    raw[:, :, 1] = stc / 2  # second group at half efficiency
    raw[:, 44, :] = 0.0  # horizon: no efficiency
    matrix_path = tmp_path / "efficiency_matrix.mat"
    _write_matrix(matrix_path, raw)

    table = load_team_efficiency_matrix(matrix_path, stc_efficiency=stc)
    assert isinstance(table, AngularResponseTable)
    assert table.values.shape == (AZIMUTH_BINS, ZENITH_BINS, 4)
    assert float(table.values[0, 0, 0]) == pytest.approx(1.0, abs=1e-6)
    assert float(table.values[0, 0, 1]) == pytest.approx(0.5, abs=1e-6)
    assert float(table.values[0, 44, 0]) == 0.0
    assert table.source == "measured"
    assert table.is_measured is True


def test_a_matrix_exceeding_stc_by_more_than_5_percent_is_refused(tmp_path: Path) -> None:
    """If the matrix has values above STC the semantics do not match and
    normalising would silently rescale physics. Refuse rather than compute
    nonsense."""
    stc = 0.27
    raw = np.full((AZIMUTH_BINS, ZENITH_BINS, 2), 0.40, dtype=np.float32)  # 48 % above STC
    path = tmp_path / "bad.mat"
    _write_matrix(path, raw)
    with pytest.raises(ModelConfigurationError, match="exceeds"):
        load_team_efficiency_matrix(path, stc_efficiency=stc)


def test_a_small_overshoot_within_5_percent_is_accepted_and_clipped(tmp_path: Path) -> None:
    """Floating-point noise and interpolation can nudge values a fraction
    above STC. That is not a semantic mismatch; clip and continue."""
    stc = 0.27
    raw = np.full((AZIMUTH_BINS, ZENITH_BINS, 2), stc * 1.02, dtype=np.float32)
    path = tmp_path / "nearly.mat"
    _write_matrix(path, raw)
    table = load_team_efficiency_matrix(path, stc_efficiency=stc)
    assert float(table.values.max()) == pytest.approx(1.0, abs=1e-6)


def test_zero_stc_efficiency_is_refused(tmp_path: Path) -> None:
    """Division by zero would silently produce inf; refuse at the input."""
    raw = np.full((AZIMUTH_BINS, ZENITH_BINS, 2), 0.1, dtype=np.float32)
    path = tmp_path / "x.mat"
    _write_matrix(path, raw)
    with pytest.raises(ModelConfigurationError, match="must be > 0"):
        load_team_efficiency_matrix(path, stc_efficiency=0.0)


def test_wrong_grid_shape_is_refused_rather_than_resampled(tmp_path: Path) -> None:
    """A matrix with the wrong azimuth or zenith resolution is not the same
    data; using it would silently discard orientation information."""
    raw = np.full((90, 45, 2), 0.1, dtype=np.float32)  # 90 azimuth bins, wrong
    path = tmp_path / "wrong.mat"
    _write_matrix(path, raw)
    with pytest.raises(ModelConfigurationError, match="does not match"):
        load_team_efficiency_matrix(path, stc_efficiency=0.27)


def test_a_two_d_matrix_is_refused(tmp_path: Path) -> None:
    """A single-cell flattening would still produce usable code paths but
    discard the per-group geometry. Refuse."""
    raw = np.full((AZIMUTH_BINS, ZENITH_BINS), 0.1, dtype=np.float32)
    path = tmp_path / "flat.mat"
    _write_matrix(path, raw)
    with pytest.raises(ModelConfigurationError, match="expected one 3-D"):
        load_team_efficiency_matrix(path, stc_efficiency=0.27)


def test_enrich_array_attaches_the_matrix_to_the_arrayconfig(tmp_path: Path) -> None:
    """The one-call convenience: a nameplate-only ArrayConfig gains per-cell
    geometry without any other field changing."""
    stc = DEFAULT_ARRAY.cell_efficiency_stc
    raw = np.full((AZIMUTH_BINS, ZENITH_BINS, 4), stc * 0.75, dtype=np.float32)
    path = tmp_path / "matrix.mat"
    _write_matrix(path, raw)
    enriched = enrich_array_with_team_matrix(DEFAULT_ARRAY, path)
    assert enriched.angular_response is not None
    assert isinstance(enriched.angular_response, AngularResponseTable)
    assert enriched.angular_response.is_measured
    # Everything else is bit-identical.
    assert enriched.area_m2 == DEFAULT_ARRAY.area_m2
    assert enriched.cell_efficiency_stc == DEFAULT_ARRAY.cell_efficiency_stc
    assert enriched.mppt_efficiency == DEFAULT_ARRAY.mppt_efficiency


def test_the_provenance_notes_record_what_was_done(tmp_path: Path) -> None:
    """Attribution and transformation travel with the matrix: a reviewer must
    be able to tell this is normalised team output, not a measurement."""
    stc = 0.27
    raw = np.full((AZIMUTH_BINS, ZENITH_BINS, 2), stc, dtype=np.float32)
    path = tmp_path / "pr.mat"
    _write_matrix(path, raw)
    table = load_team_efficiency_matrix(path, stc_efficiency=stc)
    assert "shadow-analysis" in table.notes or "JU Solar Team" in table.notes
    assert "stc_efficiency" in table.notes or "normalised" in table.notes.lower()
