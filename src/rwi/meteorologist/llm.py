"""LLM classifier for the /ask endpoint.

The adapter is INTENTIONALLY narrow: it reads the user's free-text
question and returns a (:class:`QuestionKind`, variable) tuple that is
then fed to the deterministic composer. The model never touches the
briefing's numbers, never paraphrases its prose, and never names a
tool - it only names the question type.

Why this shape: the only honest way to let a language model near a
race's weather briefing is to prevent it from being the source of any
number. A classifier can be wrong (we fall back to RISK_AHEAD) but
cannot invent "the wind will be 42 m/s". The six-section Briefing stays
deterministic, cited and testable.

The Anthropic client is imported lazily: an installation without the
``anthropic`` package still boots, and the adapter reports its state
through :class:`AdapterUnavailable` so the HTTP layer can answer 501
with a useful reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from rwi.meteorologist.contract import QuestionKind

#: The deterministic composer answers exactly these kinds. FREE_FORM
#: is never produced here; if classification fails we fall back to
#: RISK_AHEAD, which is the safest default for a race context.
_ALLOWED_KINDS = {
    QuestionKind.RISK_AHEAD,
    QuestionKind.FORECAST_RELIABILITY,
    QuestionKind.MORNING_BRIEFING,
}

_CLASSIFIER_SYSTEM_PROMPT = """\
Du är en klassificerare för JU Solar Teams meteorolog-briefing. Du
får en fri fråga på svenska eller engelska. Ditt enda jobb är att
välja EN av tre förifyllda briefing-typer och, när briefingen handlar
om prognos-tillförlitlighet, välja vilken väder-variabel som avses.

Returnera ETT JSON-objekt, inget annat, med följande fält:

{
  "question_kind": "risk_ahead" | "forecast_reliability" | "morning_briefing",
  "variable": "ghi" | "dni" | "dhi" | "wind_speed_10m" | "temperature_2m" |
              "precipitation" | "visibility" | "cloud_cover"
}

Regler:
- Välj "risk_ahead" när användaren frågar om risk, hot, varningar,
  väder framför oss, säkerhet, oväder eller "vad ska vi bevaka".
- Välj "forecast_reliability" när användaren frågar hur pålitlig,
  säker, verifierad eller oprövad en prognos är.
- Välj "morning_briefing" när användaren ber om en dagsbriefing,
  översikt för dagen eller morgonrapport.
- "variable" är obligatoriskt men används bara för forecast_reliability;
  för de andra typerna returnera "ghi" som default.
- Hitta aldrig på en kategori som inte står i listan ovan.
- Du svarar ALDRIG med siffror, väderdata eller rekommendationer. Du
  klassificerar bara frågan.
"""


class AdapterUnavailable(RuntimeError):
    """The adapter cannot answer in this environment.

    Thrown when the ``anthropic`` package is not installed, or when
    :attr:`rwi.config.Settings.anthropic_api_key` is not set. The HTTP
    layer maps this to a 501 so the dashboard can offer the operator a
    way to enable the capability rather than silently show nothing.
    """


@dataclass(frozen=True)
class Classification:
    """What the LLM decided the user was asking for.

    ``raw`` holds the model's literal JSON so a caller that wants to
    log it (for drift analysis) can, without having to re-parse.
    """

    question_kind: QuestionKind
    variable: str
    raw: str


def _parse_response(text: str) -> Classification:
    """Parse the model's JSON and clamp it to allowed values.

    A malformed response, a kind outside the allow-list, or a variable
    outside the expected set is normalised rather than raised - the
    worst outcome is a RISK_AHEAD briefing when the user wanted
    reliability, which still cites its sources honestly. Raising would
    deny the user a briefing for a parsing nit.
    """
    try:
        data = json.loads(text.strip().strip("`"))
    except json.JSONDecodeError:
        return Classification(
            question_kind=QuestionKind.RISK_AHEAD, variable="ghi", raw=text
        )
    raw_kind = str(data.get("question_kind", "")).strip().lower()
    try:
        kind = QuestionKind(raw_kind)
    except ValueError:
        kind = QuestionKind.RISK_AHEAD
    if kind not in _ALLOWED_KINDS:
        kind = QuestionKind.RISK_AHEAD
    variable = str(data.get("variable", "ghi")).strip().lower() or "ghi"
    return Classification(question_kind=kind, variable=variable, raw=text)


class AnthropicClassifier:
    """Thin wrapper around the Anthropic Messages API.

    Instantiating the classifier is cheap; it does not contact the API
    until :meth:`classify` is called. Passing ``client=`` lets tests
    inject a fake with a ``messages.create(...)`` method that returns
    an object exposing ``content[0].text``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: object | None = None,
    ) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise AdapterUnavailable(
                    "anthropic package is not installed; add it to pyproject "
                    "and 'uv pip install -e .' to enable /ask"
                ) from exc
            client = anthropic.Anthropic(api_key=api_key)
        self._client: Any = client
        self._model = model

    def classify(self, question: str) -> Classification:
        """Return a :class:`Classification` for ``question``.

        A model error (network, auth, rate limit) is surfaced as
        :class:`AdapterUnavailable` so the HTTP endpoint can answer
        503 or 501 with the real reason rather than a plausible-looking
        fake classification.
        """
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=200,
                system=_CLASSIFIER_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": question}],
            )
        except Exception as exc:
            raise AdapterUnavailable(
                f"anthropic classifier call failed: {type(exc).__name__}: {exc}"
            ) from exc
        content = getattr(response, "content", None)
        if not content:
            raise AdapterUnavailable(
                "anthropic classifier returned an empty response"
            )
        text = getattr(content[0], "text", None)
        if not isinstance(text, str):
            raise AdapterUnavailable(
                "anthropic classifier returned a non-text content block"
            )
        return _parse_response(text)


def build_classifier(
    *, api_key: str | None, model: str, client: object | None = None
) -> AnthropicClassifier:
    """Factory used by the API router.

    Raises :class:`AdapterUnavailable` when ``api_key`` is empty so the
    router can map that to 501 before any network call happens.
    """
    if not api_key and client is None:
        raise AdapterUnavailable(
            "RWI_ANTHROPIC_API_KEY is not set; /ask is disabled by configuration"
        )
    return AnthropicClassifier(
        api_key=api_key or "test", model=model, client=client
    )


__all__ = [
    "AdapterUnavailable",
    "AnthropicClassifier",
    "Classification",
    "build_classifier",
]
