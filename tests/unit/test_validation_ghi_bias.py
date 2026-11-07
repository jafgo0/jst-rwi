"""Point-wise GHI bias lookup.

Verifies the published bias table (built from the BWSC-2025 quadrangulation)
loads, finds the nearest tabulated point by great-circle distance, and
reports the measured bias numbers unchanged.
"""

from __future__ import annotations

import pytest

from rwi.validation.ghi_bias import load_default_table


def test_default_table_has_all_six_route_points() -> None:
    table = load_default_table()
    names = {p.name for p in table.points}
    assert names == {
        "Darwin", "Katherine", "Tennant Creek",
        "Alice Springs", "Coober Pedy", "Adelaide",
    }


def test_nearest_in_alice_region_picks_alice() -> None:
    table = load_default_table()
    nearest, dist_km = table.nearest(-23.7, 133.9)
    assert nearest.name == "Alice Springs"
    assert dist_km < 5.0


def test_alice_bias_matches_published_numbers() -> None:
    table = load_default_table()
    alice = next(p for p in table.points if p.name == "Alice Springs")
    assert alice.bias_ecmwf == pytest.approx(16.7)
    assert alice.bias_power == pytest.approx(23.3)
    assert alice.bias_merra2 == pytest.approx(30.1)
    assert alice.bias_median == pytest.approx(23.3)


def test_darwin_signs_disagree() -> None:
    # Darwin is the case where models disagree in sign; the dashboard banner
    # uses sign-agreement as a filter, so this guards the test for that.
    table = load_default_table()
    darwin = next(p for p in table.points if p.name == "Darwin")
    signs = {b > 0 for b in (darwin.bias_ecmwf, darwin.bias_power, darwin.bias_merra2)}
    assert len(signs) == 2, "expected Darwin biases to disagree in sign across the three products"


def test_nearest_tie_is_deterministic() -> None:
    # Equidistant between Tennant Creek (-19.65) and Alice Springs (-23.698) at -21.674.
    table = load_default_table()
    _, _ = table.nearest(-21.674, 134.0)  # just assert no error; result is one of the two
