"""Display types for the meteorologist briefing.

These are LOCAL to the meteorologist layer. They never enter
``rwi.contracts`` because they describe a presentation, not the data an
engine produced. A number in any of these types is always a number some
engine computed and that lives in the attached :class:`Citation` list.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class QuestionKind(StrEnum):
    """The three deterministic entry points answered without an LLM."""

    RISK_AHEAD = "risk_ahead"
    FORECAST_RELIABILITY = "forecast_reliability"
    MORNING_BRIEFING = "morning_briefing"
    FREE_FORM = "free_form"


class Citation(BaseModel):
    """Where a number in the briefing came from.

    Carries enough provenance that a reader can walk back to the engine
    that produced the value. ``source_module`` is the Python module path,
    not a human label.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    id: str = Field(description="short id referenced from DataPoint.citation_id")
    tool: str = Field(description="meteorologist tool that fetched the value")
    source_module: str = Field(description="python module path of the engine")
    description: str
    valid_time_utc: datetime | None = None
    retrieved_at_utc: datetime | None = None
    provider: str | None = None
    model_name: str | None = None
    model_init_time_utc: datetime | None = None


class DataPoint(BaseModel):
    """One supporting value for the briefing.

    Every ``value`` is a float produced by an engine; the ``citation_id``
    binds it to the :class:`Citation` that shows where it came from. The
    unit is required so a reader never has to guess.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    value: float
    unit: str
    valid_time_utc: datetime | None = None
    citation_id: str


class UncertaintyItem(BaseModel):
    """One piece of the briefing's uncertainty section.

    ``kind`` identifies the mechanism so the UI can group these honestly:
    ensemble spread is not model disagreement is not verified skill. When
    none of these are available ``kind='unproven'`` carries the explicit
    "no evidence" state instead of a silent omission.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal[
        "ensemble_spread",
        "model_disagreement",
        "verified_skill",
        "unproven",
        "data_gap",
        "track_record",
    ]
    statement: str
    citation_id: str | None = None


class WatchItem(BaseModel):
    """One thing to watch after this briefing.

    ``trigger`` is the condition a future refresh would flag; ``why``
    says what decision it affects.
    """

    model_config = ConfigDict(frozen=True)

    what: str
    when: str = Field(description="free-form time hint, e.g. 'nästa timme', 'vid soluppgång'")
    trigger: str
    why: str


class Briefing(BaseModel):
    """The six-section briefing, invariant-checked at construction.

    Invariants (enforced in :mod:`rwi.meteorologist.numeric_guard` when
    the briefing is rendered to prose):

    * every :class:`DataPoint` carries a ``citation_id`` resolvable in
      ``citations``;
    * ``uncertainty`` is non-empty: a briefing that found no uncertainty
      information carries one ``kind='unproven'`` item saying so;
    * ``unknowns`` lists every data gap the composer saw, including
      static ones (no telemetry, no pyranometer) that are always true
      on this platform today;
    * every section is filled with the composer's own fixed Swedish
      phrasing - the LLM adapter paraphrases later and the numeric
      guard rejects any decimal it introduced that is not in a citation.
    """

    model_config = ConfigDict(frozen=True)

    question_kind: QuestionKind
    question: str
    asked_at_utc: datetime
    assessment: str = Field(description="section 1: short bedömning")
    supporting_data: list[DataPoint] = Field(
        default_factory=list, description="section 2: stöddata med tidsstämpel"
    )
    uncertainty: list[UncertaintyItem] = Field(
        description="section 3: relevant osäkerhet; never empty"
    )
    operational_consequences: list[str] = Field(
        default_factory=list, description="section 4: operativa konsekvenser"
    )
    watch_next: list[WatchItem] = Field(
        default_factory=list, description="section 5: nästa att bevaka"
    )
    warnings_and_limits: list[str] = Field(
        default_factory=list, description="section 6: varningar och begränsningar"
    )
    citations: list[Citation] = Field(default_factory=list)
    unknowns: list[str] = Field(
        default_factory=list,
        description="explicit 'this we do not know' statements - never silently omitted",
    )

    def citation(self, citation_id: str) -> Citation | None:
        """Look up a citation by its id, or None if absent."""
        for c in self.citations:
            if c.id == citation_id:
                return c
        return None


__all__ = [
    "Briefing",
    "Citation",
    "DataPoint",
    "QuestionKind",
    "UncertaintyItem",
    "WatchItem",
]
