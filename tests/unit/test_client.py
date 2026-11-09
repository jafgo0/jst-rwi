"""The in-tree RwiClient wired to the FastAPI app via starlette TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rwi.api.app import create_app
from rwi.client.client import RwiClient, RwiClientError


@pytest.fixture
def client() -> RwiClient:
    app = create_app()
    http = TestClient(app, headers={"User-Agent": "rwi-client-test/0"})
    return RwiClient(base_url="http://testserver", _http=http)


def test_state_returns_a_dict(client: RwiClient) -> None:
    state = client.state()
    assert isinstance(state, dict)


def test_check_product_version_reads_header(client: RwiClient) -> None:
    v = client.check_product_version()
    assert v
    assert v.count(".") >= 1


def test_risk_ahead_parses(client: RwiClient) -> None:
    briefing = client.risk_ahead(hours=6.0)
    assert briefing["question_kind"] == "risk_ahead"
    assert "assessment" in briefing
    assert "uncertainty" in briefing


def test_morning_briefing_track_record_row_is_first(client: RwiClient) -> None:
    briefing = client.morning_briefing(hours=12.0)
    assert briefing["uncertainty"][0]["kind"] == "track_record"


def test_client_raises_on_bad_request(client: RwiClient) -> None:
    with pytest.raises(RwiClientError):
        client.ask("", hours=12.0)
