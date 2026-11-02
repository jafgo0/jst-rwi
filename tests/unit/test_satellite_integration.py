"""Cross-module invariants for the satellite stack.

These exist because the three satellite modules were built in parallel against
a contract, which is fast but lets the same idea get implemented twice. Each
test here pins a rule that spans module boundaries and that no single module's
own tests would catch.
"""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

SATELLITE_DIR = Path(__file__).resolve().parents[2] / "src" / "rwi" / "satellite"


def test_the_cloud_to_clear_sky_relation_exists_exactly_once() -> None:
    """One relation, one implementation.

    The nowcast module briefly carried its own copy of the Heliosat piecewise
    map, written from memory. Two copies of a relation that is not yet verified
    against its primary source is worse than one: they drift, and a correction
    applied to the authoritative copy would silently leave the other on the old
    numbers. The verification record lives in cloudindex.py, so the numbers
    must live there too.
    """
    offenders: list[str] = []
    for path in SATELLITE_DIR.glob("*.py"):
        if path.name == "cloudindex.py":
            continue
        text = path.read_text()
        # The quadratic branch's coefficients are the relation's fingerprint.
        for marker in ("2.0667", "3.6667", "1.6667"):
            if marker in text:
                offenders.append(f"{path.name} contains {marker}")
    assert not offenders, (
        "the n -> k* coefficients appear outside cloudindex.py: "
        + "; ".join(offenders)
        + ". Delegate to rwi.satellite.cloudindex.clear_sky_index instead."
    )


def test_the_nowcast_delegates_rather_than_reimplementing() -> None:
    from rwi.satellite.cloudindex import clear_sky_index
    from rwi.satellite.nowcast import default_clear_sky_index

    n = np.array([-0.5, -0.2, 0.0, 0.4, 0.8, 1.0, 1.1, 1.5])
    np.testing.assert_allclose(default_clear_sky_index(n), clear_sky_index(n))


def test_no_satellite_group_hard_couples_to_another_at_import_time() -> None:
    """Groups stay independent at import time; within a group, imports are fine.

    The three groups were built in parallel by different engineers against the
    contract. Importing a sibling inside your own group is ordinary structure
    (`retrieval` uses `cloudindex`). Importing ACROSS groups at module level is
    what would have made parallel work impossible and what makes one group's
    breakage take the others down — so delegation across a boundary is done
    with a lazy import inside the function that needs it.
    """
    groups = {
        "ingest": {"source", "tiles", "geo", "scene"},
        "irradiance": {"cloudindex", "retrieval"},
        "nowcast": {"motion", "nowcast"},
    }
    owner = {module: group for group, modules in groups.items() for module in modules}

    violations: list[str] = []
    for path in sorted(SATELLITE_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        mine = owner.get(path.stem)
        if mine is None:
            continue
        tree = ast.parse(path.read_text())
        imported: list[str] = []
        for node in tree.body:  # module level only, not inside functions
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "rwi.satellite."
            ):
                imported.append((node.module or "").split(".")[-1])
            if isinstance(node, ast.Import):
                imported.extend(
                    a.name.split(".")[-1]
                    for a in node.names
                    if a.name.startswith("rwi.satellite.")
                )
        violations.extend(
            f"{path.name} ({mine}) imports {target} ({owner[target]}) at module level"
            for target in imported
            if owner.get(target) not in (None, mine)
        )
    assert not violations, "; ".join(violations)


def test_a_satellite_irradiance_can_never_be_labelled_an_observation() -> None:
    """The platform's hardest rule, at the boundary where it is most tempting
    to break: the image really was measured, just not at ground level."""
    from datetime import UTC, datetime

    from rwi.contracts import IrradianceNowcast
    from rwi.core.geo import GeoPoint
    from rwi.core.provenance import DataKind, Provenance

    now = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    common = {
        "point": GeoPoint(latitude_deg=-23.7, longitude_deg=133.9),
        "issued_at_utc": now,
        "valid_times_utc": [now],
        "ghi_w_m2": [820.0],
        "clear_sky_ghi_w_m2": [900.0],
        "clear_sky_index": [0.91],
        "horizon_s": 3600.0,
        "motion": None,
        "method": "test",
        "provenance": Provenance(
            provider="himawari",
            dataset="test",
            kind=DataKind.DERIVED,
            retrieved_at_utc=now,
        ),
    }
    with pytest.raises(ValueError, match="not an observation"):
        IrradianceNowcast(**common, kind=DataKind.OBSERVATION)  # type: ignore[arg-type]

    allowed = IrradianceNowcast(**common)  # type: ignore[arg-type]
    assert allowed.kind is DataKind.DERIVED
    assert "NOT VALIDATED" in allowed.validation_status


def test_every_nowcast_carries_its_limitations() -> None:
    """A number without its caveats is the failure mode this whole module is
    built to avoid, so the caveats travel on the object rather than in a doc."""
    from rwi.satellite.nowcast import DEFAULT_IMAGE_LATENCY_S, MAX_EFFECTIVE_LEAD_S

    # The latency is not a guess: it was measured against the live bucket.
    assert DEFAULT_IMAGE_LATENCY_S == pytest.approx(840.0)
    # A two-hour ceiling on the EFFECTIVE lead, which includes image age.
    assert MAX_EFFECTIVE_LEAD_S == pytest.approx(7200.0)
    assert MAX_EFFECTIVE_LEAD_S > DEFAULT_IMAGE_LATENCY_S
