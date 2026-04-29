"""Thin httpx wrapper for Binance public REST endpoints.

We deliberately do NOT use ccxt for historical bulk fetching:
  * adds abstraction we don't need
  * harder to debug rate-limit / pagination edge cases
  * the historical fetch is one-time, not the place for an abstraction

For future live trading we may layer ccxt for venue uniformity, but FS uses
direct httpx.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Treat these as "give up immediately" — retrying won't help.
NON_RETRYABLE_STATUSES = {400, 401, 403, 404, 418, 451}
# Treat these as "back off and retry."
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class BinanceAPIError(RuntimeError):
    """Unrecoverable HTTP error from Binance — caller should abort."""


class BinanceClient:
    """Long-lived httpx.Client + retry loop. Use as a context manager."""

    def __init__(
        self,
        max_retries: int = 5,
        base_backoff_s: float = 1.0,
        max_backoff_s: float = 60.0,
        connect_timeout_s: float = 10.0,
        read_timeout_s: float = 30.0,
    ) -> None:
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=connect_timeout_s, read=read_timeout_s,
                                  write=connect_timeout_s, pool=connect_timeout_s),
            headers={"User-Agent": "alpha-factory-fs/0.1 (+research)"},
        )

    def __enter__(self) -> BinanceClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._client.close()

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        """GET with retry on transient errors. Raises BinanceAPIError on hard fail."""
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                r = self._client.get(url, params=params)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout,
                    httpx.PoolTimeout, httpx.RemoteProtocolError) as e:
                last_err = e
                wait = self._backoff(attempt)
                log.warning("transient %s on %s, retrying in %.1fs", type(e).__name__, url, wait)
                time.sleep(wait)
                continue

            status = r.status_code
            if status == 200:
                return r.json()

            if status in NON_RETRYABLE_STATUSES:
                # 418 = IP banned; 451 = geoblock; 4xx other = bad request.
                # Surface body for debugging but do not retry.
                raise BinanceAPIError(
                    f"non-retryable status={status} url={url} params={params} body={r.text[:300]}"
                )

            if status in RETRYABLE_STATUSES:
                # Honor Retry-After if Binance sends one.
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else self._backoff(attempt)
                log.warning("status=%d on %s, backoff %.1fs (attempt %d/%d)",
                            status, url, wait, attempt + 1, self.max_retries)
                time.sleep(wait)
                continue

            # Unknown status — be paranoid and abort.
            raise BinanceAPIError(
                f"unexpected status={status} url={url} body={r.text[:300]}"
            )

        # Exhausted retries.
        raise BinanceAPIError(f"max_retries exhausted on {url}; last error: {last_err}")

    def _backoff(self, attempt: int) -> float:
        return min(self.max_backoff_s, self.base_backoff_s * (2 ** attempt))
