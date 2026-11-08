"""The angular response table: the hook for JU Solar Team's shadow-analysis.

The table encodes per-cell orientation. Loading the team's own matrix will
replace the identity default without changing a single caller. This test
module pins the shape, the invariants and the guards against the three ways
this kind of layer silently goes wrong: wrong shape, wrong sign conventions,
and a loader that quietly uses the wrong panel.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from rwi.energy.angular import (
    AZIMUTH_BINS,
    ZENITH_BINS,
    AngularResponseTable,
    identity_response,
    load_shadow_matrix,
)


def test_the_identity_response_is_1_everywhere_and_knows_it_is_a_fallback() -> None:
    table = identity_response(group_count=5)
    assert table.values.shape == (AZIMUTH_BINS, ZENITH_BINS, 5)
    assert float(table.values.min()) == 1.0
    assert float(table.values.max()) == 1.0
    # The caller must be able to distinguish identity from measured.
    assert table.is_measured is False
    assert table.source == "identity"
    assert "not measured" in table.notes.lower() or "identity" in table.notes.lower()


def test_shape_matches_the_shadow_analysis_tools_output() -> None:
    """shadow-analysis/panel_table.py:21 writes a 180 x 45 x N matrix. If we
    change our shape, a day of work loading the team's file is wasted."""
    assert AZIMUTH_BINS == 180
    assert ZENITH_BINS == 45


def test_mean_response_is_0_below_the_horizon() -> None:
    """The night-time invariant: sun below the horizon means no response, by
    physics, regardless of what the lookup table happens to contain."""
    table = identity_response(group_count=4)
    for zen in (90.0, 90.1, 120.0, 180.0):
        assert table.mean_response(azimuth_deg=0.0, zenith_deg=zen) == 0.0
    # And a negative zenith (above the sky dome) is nonsense -> 0.
    assert table.mean_response(azimuth_deg=0.0, zenith_deg=-5.0) == 0.0


def test_mean_response_wraps_azimuth_modulo_360() -> None:
    table = identity_response(group_count=1)
    for az in (0.0, 180.0, 359.9, 360.0, 720.5, -45.0):
        assert table.mean_response(azimuth_deg=az, zenith_deg=30.0) == 1.0


def test_shape_mismatch_is_refused_not_silently_summed_wrong() -> None:
    """If a 4-group matrix is given a 5-group table declaration, the per-group
    mean comes out plausible and completely wrong. Refuse at construction."""
    wrong_groups = np.ones((AZIMUTH_BINS, ZENITH_BINS, 7), dtype=np.float32)
    with pytest.raises(ValueError, match="refusing to silently sum the wrong panel"):
        AngularResponseTable(
            name="t", source="simulated", group_count=5, values=wrong_groups
        )
    wrong_grid = np.ones((100, 50, 3), dtype=np.float32)
    with pytest.raises(ValueError, match="must be 180 azimuth"):
        AngularResponseTable(
            name="t", source="simulated", group_count=3, values=wrong_grid
        )
    wrong_dim = np.ones((AZIMUTH_BINS, ZENITH_BINS), dtype=np.float32)
    with pytest.raises(ValueError, match="must be 3-D"):
        AngularResponseTable(
            name="t", source="simulated", group_count=1, values=wrong_dim
        )


def test_values_outside_the_unit_interval_are_refused() -> None:
    """A cell cannot deliver more than it receives, and cannot deliver less
    than zero. Values outside [0, 1] are a bug in the matrix generator."""
    too_high = np.ones((AZIMUTH_BINS, ZENITH_BINS, 1), dtype=np.float32) * 1.01
    with pytest.raises(ValueError, match="must lie in"):
        AngularResponseTable(
            name="t", source="simulated", group_count=1, values=too_high
        )
    with_nan = np.ones((AZIMUTH_BINS, ZENITH_BINS, 1), dtype=np.float32)
    with_nan[0, 0, 0] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        AngularResponseTable(
            name="t", source="simulated", group_count=1, values=with_nan
        )


def test_a_graded_table_averages_correctly_across_groups() -> None:
    """The mean across groups is what the array power model actually consumes.
    An unweighted table that applies a 0.5 derate to one of two equal groups
    must give a mean response of 0.75, not 0.5 or 1.0."""
    values = np.ones((AZIMUTH_BINS, ZENITH_BINS, 2), dtype=np.float32)
    values[:, :, 1] = 0.5
    table = AngularResponseTable(
        name="halved", source="simulated", group_count=2, values=values
    )
    assert table.mean_response(azimuth_deg=0.0, zenith_deg=30.0) == pytest.approx(0.75)


def test_cell_weights_switch_the_reduction_to_a_weighted_mean() -> None:
    """2027 car: 5 groups of 84/84/84/42/44 cells. If group 0 is in full shade
    the correct array-level response is weighted by cell count, not plain
    mean. Unweighted: (0 + 4*1.0) / 5 = 0.8. Weighted: (0*84 + 84 + 84 + 42
    + 44) / (84+84+84+42+44) = 254 / 338 ~= 0.7515."""
    values = np.ones((AZIMUTH_BINS, ZENITH_BINS, 5), dtype=np.float32)
    values[:, :, 0] = 0.0
    weights = (84, 84, 84, 42, 44)
    weighted = AngularResponseTable(
        name="2027", source="simulated", group_count=5, values=values,
        cell_weights=weights,
    )
    unweighted = AngularResponseTable(
        name="2027-unweighted", source="simulated", group_count=5, values=values,
    )
    assert weighted.mean_response(azimuth_deg=0.0, zenith_deg=30.0) == pytest.approx(
        254.0 / 338.0, abs=1e-6
    )
    assert unweighted.mean_response(azimuth_deg=0.0, zenith_deg=30.0) == pytest.approx(0.8)


def test_cell_weights_of_wrong_length_or_nonpositive_are_refused() -> None:
    values = np.ones((AZIMUTH_BINS, ZENITH_BINS, 3), dtype=np.float32)
    with pytest.raises(ValueError, match="cell_weights has 2 entries"):
        AngularResponseTable(
            name="t", source="simulated", group_count=3, values=values,
            cell_weights=(1, 2),
        )
    with pytest.raises(ValueError, match="cell_weights must all be > 0"):
        AngularResponseTable(
            name="t", source="simulated", group_count=3, values=values,
            cell_weights=(1, 0, 2),
        )


def test_a_measured_table_reports_itself_as_measured() -> None:
    values = np.ones((AZIMUTH_BINS, ZENITH_BINS, 5), dtype=np.float32) * 0.9
    table = AngularResponseTable(
        name="cad", source="measured", group_count=5, values=values
    )
    assert table.is_measured is True


def test_load_npy_round_trip(tmp_path: Path) -> None:
    """What the team's writer produces, our loader reads. A format change on
    their side becomes a loud error, not a silent misread."""
    original = np.random.default_rng(seed=42).uniform(
        0.0, 1.0, size=(AZIMUTH_BINS, ZENITH_BINS, 5)
    ).astype(np.float32)
    path = tmp_path / "shadow.npy"
    np.save(path, original)
    table = load_shadow_matrix(
        path, name="loaded", group_count=5, cell_weights=(84, 84, 84, 42, 44)
    )
    np.testing.assert_array_equal(table.values, original)
    assert table.source == "measured"
    assert table.is_measured is True
    assert table.cell_weights == (84, 84, 84, 42, 44)


def test_load_npy_refuses_mismatched_group_count(tmp_path: Path) -> None:
    arr = np.ones((AZIMUTH_BINS, ZENITH_BINS, 4), dtype=np.float32)
    path = tmp_path / "shadow.npy"
    np.save(path, arr)
    with pytest.raises(ValueError, match="does not match group_count"):
        load_shadow_matrix(path, name="x", group_count=5)


def test_load_rejects_unknown_file_type(tmp_path: Path) -> None:
    """We only read what the team actually writes. Guessing a format is how
    you load the wrong file and never find out."""
    bad = tmp_path / "shadow.pickle"
    bad.write_bytes(b"not real")
    with pytest.raises(ValueError, match="unsupported file type"):
        load_shadow_matrix(bad, name="x", group_count=1)


# --------------------------------------------------------------------------
# integration with array_power
#
# Not a sanity check on the response table but a sanity check that it is
# actually APPLIED by rwi.energy.array_power. A table that lives in a module
# nothing calls would be a well-tested dead end.


def test_identity_response_leaves_array_power_unchanged() -> None:
    """The default angular_response is None, which is the same model result as
    an identity response with factor 1 at every angle. Both must give exactly
    the same net power - otherwise the hook is subtly biased."""

    from rwi.contracts import DataKind, GeoPoint, POAIrradiance, Provenance
    from rwi.core.timebase import utc_now
    from rwi.energy.array import DEFAULT_ARRAY, array_power

    now = utc_now()
    prov = Provenance(
        provider="test",
        dataset="t",
        kind=DataKind.PHYSICAL_MODEL,
        retrieved_at_utc=now,
        point=GeoPoint(latitude_deg=-23.7, longitude_deg=133.9, elevation_m=545.0),
    )
    poa = POAIrradiance(
        time_utc=now,
        poa_global_w_m2=800.0,
        poa_direct_w_m2=700.0,
        poa_sky_diffuse_w_m2=80.0,
        poa_ground_diffuse_w_m2=20.0,
        aoi_deg=35.0,
        surface_tilt_deg=0.0,
        surface_azimuth_deg=180.0,
        albedo=0.2,
        transposition_model="haydavies",
        provenance=prov,
    )
    flat = array_power(poa, DEFAULT_ARRAY, air_temperature_c=25.0, wind_speed_mps=4.0)

    with_identity = DEFAULT_ARRAY.model_copy(
        update={"angular_response": identity_response(group_count=1)}
    )
    identity = array_power(poa, with_identity, air_temperature_c=25.0, wind_speed_mps=4.0)
    assert flat.net_power_w == pytest.approx(identity.net_power_w, abs=1e-9)


def test_a_measured_half_response_halves_the_array_power() -> None:
    """A response of 0.5 everywhere must halve the output, exactly. If this
    comes out any other way the hook is applied at the wrong stage."""
    import numpy as np

    from rwi.contracts import DataKind, GeoPoint, POAIrradiance, Provenance
    from rwi.core.timebase import utc_now
    from rwi.energy.array import DEFAULT_ARRAY, array_power

    now = utc_now()
    prov = Provenance(
        provider="test",
        dataset="t",
        kind=DataKind.PHYSICAL_MODEL,
        retrieved_at_utc=now,
        point=GeoPoint(latitude_deg=-23.7, longitude_deg=133.9, elevation_m=545.0),
    )
    poa = POAIrradiance(
        time_utc=now,
        poa_global_w_m2=800.0,
        poa_direct_w_m2=700.0,
        poa_sky_diffuse_w_m2=80.0,
        poa_ground_diffuse_w_m2=20.0,
        aoi_deg=35.0,
        surface_tilt_deg=0.0,
        surface_azimuth_deg=180.0,
        albedo=0.2,
        transposition_model="haydavies",
        provenance=prov,
    )
    flat = array_power(poa, DEFAULT_ARRAY, air_temperature_c=25.0, wind_speed_mps=4.0)

    halved = AngularResponseTable(
        name="halved",
        source="measured",
        group_count=1,
        values=np.ones((AZIMUTH_BINS, ZENITH_BINS, 1), dtype=np.float32) * 0.5,
    )
    array_half = DEFAULT_ARRAY.model_copy(update={"angular_response": halved})
    half = array_power(poa, array_half, air_temperature_c=25.0, wind_speed_mps=4.0)
    assert half.dc_power_w == pytest.approx(flat.dc_power_w * 0.5, abs=1e-9)
    assert half.net_power_w == pytest.approx(flat.net_power_w * 0.5, abs=1e-9)
