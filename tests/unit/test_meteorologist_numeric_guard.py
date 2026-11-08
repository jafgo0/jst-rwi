"""The numeric guard: no decimal literal survives without a citation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rwi.meteorologist.contract import (
    Briefing,
    Citation,
    DataPoint,
    QuestionKind,
    UncertaintyItem,
)
from rwi.meteorologist.numeric_guard import HallucinatedNumberError, check_prose

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _briefing_with(value: float) -> Briefing:
    citation = Citation(
        id="c1", tool="t", source_module="rwi.x", description="d"
    )
    return Briefing(
        question_kind=QuestionKind.RISK_AHEAD,
        question="q",
        asked_at_utc=NOW,
        assessment="a",
        supporting_data=[
            DataPoint(label="x", value=value, unit="u", citation_id="c1")
        ],
        uncertainty=[UncertaintyItem(kind="unproven", statement="s")],
        citations=[citation],
    )


def test_a_cited_number_passes() -> None:
    check_prose("Toppeffekt 1200 W förväntas.", _briefing_with(1200.0))


def test_a_rounded_cited_number_passes() -> None:
    check_prose("Vinden är ungefär 7.2 m/s.", _briefing_with(7.234))


def test_an_orphan_number_raises() -> None:
    with pytest.raises(HallucinatedNumberError, match="999"):
        check_prose("Men 999 W uppfanns.", _briefing_with(1200.0))


def test_iso_timestamps_do_not_count_as_numbers() -> None:
    check_prose(
        "Solnedgång 2026-11-08T15:00:00Z. Toppen 1200 W.", _briefing_with(1200.0)
    )


def test_small_integers_0_to_24_are_allowed() -> None:
    check_prose("Vi talar om 6 timmar framåt och 0 varningar.", _briefing_with(1200.0))
