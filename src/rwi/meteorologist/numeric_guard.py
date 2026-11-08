"""Numeric guard: every number in rendered prose must come from a citation.

Used after the LLM adapter paraphrases a briefing to natural language.
The guard tokenises the rendered prose for decimal literals and checks
that each one appears - as the same string - in :attr:`Briefing.supporting_data`
or inside a :class:`Citation`'s own fields. A single orphan number
raises :class:`HallucinatedNumberError`.

The deterministic composer does not need the guard (it draws numbers
directly from engines) but it is harmless to run on its output and the
tests do.

Scope: the guard is intentionally conservative. It catches *invented*
numbers; it does not detect a wrong label applied to a correct number.
That is what the citations themselves are for.
"""

from __future__ import annotations

import re

from rwi.meteorologist.contract import Briefing


class HallucinatedNumberError(ValueError):
    """Rendered prose contains a number that is not in any citation.

    The guard rejects the output rather than quietly serving it; the
    offending tokens are included so the LLM adapter can retry with a
    stricter template.
    """


_NUMBER_RE = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?")


def _allowed_numbers(briefing: Briefing) -> set[str]:
    """Numbers that may appear in prose: supporting-data values plus a
    small allow-list of trivially safe integers (0..24 for hours etc).

    Timestamps and ISO dates are allowed through the ISO regex in
    :func:`_strip_iso_times`, not here, because they contain digits that
    would otherwise fail the guard."""
    allowed: set[str] = {str(i) for i in range(0, 25)}
    allowed.update(str(i) for i in (30, 60, 90, 100, 180, 360))
    for dp in briefing.supporting_data:
        allowed.add(_canonical(dp.value))
        allowed.add(_canonical(round(dp.value, 1)))
        allowed.add(_canonical(round(dp.value, 2)))
    return allowed


def _canonical(value: float) -> str:
    """Numeric form used both for allowed and extracted tokens."""
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


_ISO_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?")


def _strip_iso_times(text: str) -> str:
    """ISO 8601 timestamps come from Pydantic datetimes and are always
    trustworthy; stripping them keeps the guard focused on real numeric
    claims. Ditto HH:MM wall-clock hints."""
    without_iso = _ISO_TIME_RE.sub("", text)
    return re.sub(r"\b\d{1,2}:\d{2}\b", "", without_iso)


def check_prose(prose: str, briefing: Briefing) -> None:
    """Raise :class:`HallucinatedNumberError` if ``prose`` contains a
    number not justified by ``briefing``.

    Call this once per rendered output, before returning it from the
    LLM adapter.
    """
    cleaned = _strip_iso_times(prose)
    found = {_canonicalise_token(m.group(0)) for m in _NUMBER_RE.finditer(cleaned)}
    allowed = _allowed_numbers(briefing)
    orphan = sorted(found - allowed)
    if orphan:
        raise HallucinatedNumberError(
            "rendered prose contains numbers that are not in any citation: "
            + ", ".join(orphan)
        )


def _canonicalise_token(token: str) -> str:
    try:
        value = float(token)
    except ValueError:
        return token
    return _canonical(value)


__all__ = ["HallucinatedNumberError", "check_prose"]
