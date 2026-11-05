"""Minimal Deribit public REST client: rate limiting, retries with backoff."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from mm.config import HttpConfig

log = logging.getLogger(__name__)

# JSON-RPC error codes that mean "slow down / try again" (Deribit docs: too_many_requests).
_RETRYABLE_RPC_CODES = {10028}


class DeribitError(RuntimeError):
    pass


class DeribitClient:
    def __init__(
        self,
        base_url: str,
        http: HttpConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._http = http
        self._sleep = sleep
        self._now = monotonic
        self._min_gap = 1.0 / http.max_requests_per_s
        self._last: float | None = None
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=http.timeout_s,
            transport=transport,
            headers={"User-Agent": "eth-options-mm-research/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DeribitClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _throttle(self) -> None:
        if self._last is not None:
            wait = self._min_gap - (self._now() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._now()

    def _backoff(self, attempt: int) -> float:
        return min(self._http.backoff_base_s * 2**attempt, self._http.backoff_max_s)

    def get(self, method: str, params: dict[str, Any]) -> Any:
        """Call a public method, e.g. get('public/get_instruments', {...}); returns `result`."""
        q = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items()}
        last_err = "no attempt made"
        for attempt in range(self._http.max_retries + 1):
            if attempt:
                self._sleep(self._backoff(attempt - 1))
            self._throttle()
            try:
                resp = self._client.get(method.lstrip("/"), params=q)
            except httpx.TransportError as e:
                last_err = f"transport error: {e!r}"
                log.warning("%s %s (attempt %d)", method, last_err, attempt + 1)
                continue

            body = None
            with contextlib.suppress(ValueError):
                body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None

            if (
                resp.status_code == 429
                or resp.status_code >= 500
                or (err and err.get("code") in _RETRYABLE_RPC_CODES)
            ):
                last_err = f"HTTP {resp.status_code} {err or ''}".strip()
                log.warning("%s %s (attempt %d)", method, last_err, attempt + 1)
                continue
            if err:
                raise DeribitError(
                    f"{method}: {err.get('code')} {err.get('message')} {err.get('data', '')}"
                )
            if resp.status_code != 200 or not isinstance(body, dict) or "result" not in body:
                raise DeribitError(
                    f"{method}: unexpected HTTP {resp.status_code}: {resp.text[:200]}"
                )
            return body["result"]
        raise DeribitError(
            f"{method}: gave up after {self._http.max_retries + 1} attempts ({last_err})"
        )
