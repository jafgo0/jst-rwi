"""A minimal, read-only HTTP client for jst-rwi's STABLE surface.

Design choices made explicit:

* No retries that hide 5xx. A failure that the server could not recover
  from is surfaced, not papered over.
* No silent version upgrades. ``check_product_version`` returns the
  server's ``X-Rwi-Product-Version`` and warns when the major component
  is ahead of what the client was built against.
* Return values are raw ``dict`` payloads. Optional helpers parse them
  into :mod:`rwi.contracts` models for ergonomics, but this is a
  convenience; see the package docstring.
"""

from __future__ import annotations

import warnings
from typing import Any

import httpx

CLIENT_COMPATIBLE_MAJOR = 0
"""The product major version this client was built against.

Pre-1.0 the minor (``0.y.0``) is also a breaking surface, so a mismatch
on ``minor`` is also reported.
"""


class RwiClientError(RuntimeError):
    """Any failure talking to the jst-rwi product surface.

    Wraps transport failures, non-2xx responses and malformed JSON so a
    caller has one exception type to guard against. The original
    exception is attached as ``__cause__``.
    """


class RwiClient:
    """Read-only HTTP client for the jst-rwi product surface.

    ``base_url`` is the server origin (no trailing slash needed).
    ``user_agent`` should identify the calling tool - the server logs
    it and operators read the logs.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 10.0,
        user_agent: str = "rwi-client/0",
        _http: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        if _http is not None:
            self._client = _http
        else:
            self._client = httpx.Client(
                base_url=self._base_url,
                timeout=timeout_s,
                headers={"User-Agent": user_agent, "Accept": "application/json"},
            )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RwiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- product metadata ------------------------------------------------

    def check_product_version(self) -> str:
        """Return the server's product version; warn on major/minor skew.

        Reads ``X-Rwi-Product-Version`` from an inexpensive STABLE
        endpoint (``/api/state``). Does not raise on skew; the caller
        decides whether a warning is fatal for their use case.
        """
        resp = self._client.get("/api/state")
        version = resp.headers.get("X-Rwi-Product-Version", "")
        if not version:
            warnings.warn(
                "server did not return X-Rwi-Product-Version; is this a jst-rwi server?",
                stacklevel=2,
            )
            return version
        parts = version.split(".")
        try:
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            warnings.warn(f"malformed product version {version!r}", stacklevel=2)
            return version
        if major != CLIENT_COMPATIBLE_MAJOR:
            warnings.warn(
                f"jst-rwi server is at major {major}, client built for "
                f"major {CLIENT_COMPATIBLE_MAJOR} - responses may not parse",
                stacklevel=2,
            )
        elif major == 0 and minor != 0:
            warnings.warn(
                f"jst-rwi pre-1.0 minor skew: server {version}; "
                "every 0.y.0 is a breaking release",
                stacklevel=2,
            )
        return version

    # ---- STABLE endpoints ------------------------------------------------

    def state(self) -> dict[str, Any]:
        return self._get("/api/state")

    def risk_ahead(self, *, hours: float = 12.0) -> dict[str, Any]:
        return self._get("/api/meteorologist/risk-ahead", params={"hours": hours})

    def forecast_reliability(
        self, *, variable: str = "ghi", hours: float = 6.0
    ) -> dict[str, Any]:
        return self._get(
            "/api/meteorologist/forecast-reliability",
            params={"variable": variable, "hours": hours},
        )

    def morning_briefing(self, *, hours: float = 12.0) -> dict[str, Any]:
        return self._get(
            "/api/meteorologist/morning-briefing", params={"hours": hours}
        )

    def ask(self, question: str, *, hours: float = 12.0) -> dict[str, Any]:
        return self._post(
            "/api/meteorologist/ask",
            json={"question": question, "hours": hours},
        )

    def route_weather(self, route_id: str) -> dict[str, Any]:
        return self._get(f"/api/route/{route_id}/weather")

    def route_progress(self, route_id: str) -> dict[str, Any]:
        return self._get(f"/api/route/{route_id}/progress")

    def forecast_quality_archive(self) -> dict[str, Any]:
        return self._get("/api/forecast/archive")

    def forecast_quality_verification(
        self, *, variables: list[str] | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if variables:
            params["variable"] = variables
        return self._get("/api/forecast/verification", params=params)

    # ---- transport -------------------------------------------------------

    def _get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise RwiClientError(f"GET {path} failed: {exc}") from exc
        return self._parse(resp, path)

    def _post(self, path: str, *, json: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(path, json=json)
        except httpx.HTTPError as exc:
            raise RwiClientError(f"POST {path} failed: {exc}") from exc
        return self._parse(resp, path)

    def _parse(self, resp: httpx.Response, path: str) -> dict[str, Any]:
        if resp.status_code >= 500:
            raise RwiClientError(
                f"{path} → {resp.status_code}: server error, not retried"
            )
        if resp.status_code >= 400:
            raise RwiClientError(
                f"{path} → {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise RwiClientError(f"{path} returned non-JSON body") from exc
        if not isinstance(data, dict):
            raise RwiClientError(
                f"{path} returned {type(data).__name__}, expected object"
            )
        return data
