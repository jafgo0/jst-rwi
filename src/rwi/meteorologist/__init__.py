"""The meteorologist layer: a tool-using façade over the existing engines.

Fas 1 scope: the deterministic composer and its tool adapters, plus the
three fixed-question endpoints. The LLM adapter and /ask endpoint are
planned for the same fas and live behind a capability gate so that a
fresh install without an Anthropic key gets 501, never fabricated prose.

See ``docs/science/meteorologist.md`` (planned) for the principle of
the layer: it CITES, it does not COMPUTE.
"""

from rwi.meteorologist.composer import BriefingContext, compose
from rwi.meteorologist.contract import (
    Briefing,
    Citation,
    DataPoint,
    QuestionKind,
    UncertaintyItem,
    WatchItem,
)
from rwi.meteorologist.numeric_guard import HallucinatedNumberError, check_prose
from rwi.meteorologist.tools import ToolResult

__all__ = [
    "Briefing",
    "BriefingContext",
    "Citation",
    "DataPoint",
    "HallucinatedNumberError",
    "QuestionKind",
    "ToolResult",
    "UncertaintyItem",
    "WatchItem",
    "check_prose",
    "compose",
]
