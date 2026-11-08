"""The LLM classifier for /api/meteorologist/ask.

The classifier is intentionally tiny: it picks a :class:`QuestionKind`
and a variable from a free-text question. These tests verify:
* parsing of a well-formed response;
* parsing of malformed / out-of-range responses falls back to
  RISK_AHEAD rather than raising;
* ``build_classifier`` refuses without an API key (unless a fake client
  is injected);
* model errors surface as :class:`AdapterUnavailable` rather than
  plausible fake classifications.

No network: the Anthropic client is stubbed with a fake exposing the
same ``messages.create`` → ``content[0].text`` shape.
"""

from __future__ import annotations

import pytest

from rwi.meteorologist.contract import QuestionKind
from rwi.meteorologist.llm import (
    AdapterUnavailable,
    AnthropicClassifier,
    _parse_response,
    build_classifier,
)


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _FakeMessages:
    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self._text = text
        self._exc = exc
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return _Response(self._text or "")


class _FakeClient:
    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self.messages = _FakeMessages(text=text, exc=exc)


def test_parse_valid_response_returns_kind_and_variable() -> None:
    cls = _parse_response(
        '{"question_kind": "forecast_reliability", "variable": "ghi"}'
    )
    assert cls.question_kind is QuestionKind.FORECAST_RELIABILITY
    assert cls.variable == "ghi"


def test_parse_malformed_json_falls_back_to_risk_ahead() -> None:
    cls = _parse_response("not json at all")
    assert cls.question_kind is QuestionKind.RISK_AHEAD
    assert cls.variable == "ghi"


def test_parse_free_form_kind_is_clamped_to_risk_ahead() -> None:
    # free_form is a valid enum but NOT in the deterministic allow-list,
    # so the classifier must not route to it (the composer raises on it).
    cls = _parse_response('{"question_kind": "free_form", "variable": "ghi"}')
    assert cls.question_kind is QuestionKind.RISK_AHEAD


def test_parse_unknown_kind_is_clamped_to_risk_ahead() -> None:
    cls = _parse_response('{"question_kind": "nonsense", "variable": "wind_speed_10m"}')
    assert cls.question_kind is QuestionKind.RISK_AHEAD
    assert cls.variable == "wind_speed_10m"


def test_build_classifier_without_key_and_without_client_raises() -> None:
    with pytest.raises(AdapterUnavailable):
        build_classifier(api_key=None, model="claude-haiku-5-5")


def test_build_classifier_with_injected_client_works_without_key() -> None:
    fake = _FakeClient(text='{"question_kind": "morning_briefing", "variable": "ghi"}')
    classifier = build_classifier(
        api_key=None, model="claude-haiku-5-5", client=fake
    )
    cls = classifier.classify("vad händer idag?")
    assert cls.question_kind is QuestionKind.MORNING_BRIEFING
    # The classifier sends the user's question through unchanged.
    assert fake.messages.calls[0]["messages"][0]["content"] == "vad händer idag?"


def test_classifier_surfaces_upstream_errors_as_adapter_unavailable() -> None:
    fake = _FakeClient(exc=RuntimeError("rate limited"))
    classifier = AnthropicClassifier(
        api_key="test", model="claude-haiku-5-5", client=fake
    )
    with pytest.raises(AdapterUnavailable, match="rate limited"):
        classifier.classify("vad är viktigast?")


def test_classifier_rejects_empty_content() -> None:
    class _Empty:
        def __init__(self) -> None:
            self.content: list = []

    class _Client:
        class messages:
            @staticmethod
            def create(**_):
                return _Empty()

    classifier = AnthropicClassifier(
        api_key="test", model="claude-haiku-5-5", client=_Client()
    )
    with pytest.raises(AdapterUnavailable, match="empty response"):
        classifier.classify("vad är viktigast?")
