"""Dashboard files must be revalidated, so a browser never runs a stale module."""

from fastapi.testclient import TestClient

from rwi.api.app import create_app


def test_dashboard_files_are_served_no_cache() -> None:
    client = TestClient(create_app())
    for path in ("/", "/js/main.js", "/js/views/command.js"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["Cache-Control"] == "no-cache", path


def test_revalidation_still_yields_304() -> None:
    client = TestClient(create_app())
    first = client.get("/js/main.js")
    again = client.get("/js/main.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
