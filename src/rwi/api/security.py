"""Shared-secret check and a small in-process rate limit for costly endpoints.

Endpoints that claim to be the car (``live_gps``), spend provider bandwidth
(capture) or spend Anthropic tokens (``/ask``) all require the same
``X-Telemetry-Secret`` header. The comparison is constant-time.
"""

import hmac
import threading
import time
from collections import deque

from fastapi import HTTPException, status

from rwi.config import Settings


def require_shared_secret(settings: Settings, secret: str | None, *, action: str) -> None:
    """Return None when ``secret`` matches the configured one; raise HTTP 403 otherwise."""
    expected = settings.telemetry_shared_secret
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"{action} is not accepted on this deployment because "
                "RWI_TELEMETRY_SHARED_SECRET is not set. Configure the secret and "
                "resend with the X-Telemetry-Secret header."
            ),
        )
    if not secret or not hmac.compare_digest(secret.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{action} requires a correct X-Telemetry-Secret header.",
        )


class SlidingWindowLimiter:
    """At most ``limit`` calls per ``window_s`` seconds, process-wide."""

    def __init__(self, window_s: float = 60.0) -> None:
        self._window_s = window_s
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, limit: int) -> float | None:
        """Return None when a call is allowed now, else seconds until the next slot."""
        now = time.monotonic()
        with self._lock:
            while self._calls and now - self._calls[0] >= self._window_s:
                self._calls.popleft()
            if limit <= 0 or len(self._calls) >= limit:
                retry = self._window_s - (now - self._calls[0]) if self._calls else self._window_s
                return max(retry, 0.0)
            self._calls.append(now)
            return None

    def reset(self) -> None:
        """Forget every recorded call (tests)."""
        with self._lock:
            self._calls.clear()


ASK_LIMITER = SlidingWindowLimiter()


def enforce_rate_limit(limiter: SlidingWindowLimiter, limit: int, *, action: str) -> None:
    """Return None when allowed; raise HTTP 429 with Retry-After otherwise."""
    retry = limiter.acquire(limit)
    if retry is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": f"{action}: at most {limit} requests per minute",
            },
            headers={"Retry-After": str(max(1, round(retry)))},
        )
