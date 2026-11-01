"""Quality-control report types."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from rwi.core.provenance import USABLE_FLAGS, DataKind, QualityFlag

__all__ = ["QualityIssue", "QualityReport"]


class QualityIssue(BaseModel):
    """One finding of one check. A value can have several; the value itself keeps
    only the most severe flag, the report keeps them all."""

    model_config = ConfigDict(frozen=True)

    check: str = Field(description="machine name, e.g. 'range', 'rate', 'dew_point_le_temperature'")
    flag: QualityFlag
    variable: str | None = Field(default=None, description="None for series-level findings")
    time_utc: datetime | None = None
    detail: str


class QualityReport(BaseModel):
    """What the quality layer found in one series. Flags only - nothing is changed
    or removed; deciding what to do is the job of the layer above."""

    model_config = ConfigDict(frozen=True)

    provider: str
    dataset: str
    kind: DataKind
    checked_at_utc: datetime
    step_count: int
    value_count: int
    flag_counts: dict[str, int] = Field(
        description="count of values per QualityFlag value (all flags present, zeros included)"
    )
    by_variable: dict[str, dict[str, int]] = Field(
        description="variable -> flag value -> count (non-zero entries only)"
    )
    usable_count: int
    usable_fraction: float = Field(ge=0.0, le=1.0)
    issues: list[QualityIssue] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True when every value carries a usable flag and no series-level issue exists."""
        flagged = sum(n for f, n in self.flag_counts.items() if QualityFlag(f) not in USABLE_FLAGS)
        return flagged == 0 and not any(i.variable is None for i in self.issues)

    def count(self, flag: QualityFlag) -> int:
        return self.flag_counts.get(flag.value, 0)
