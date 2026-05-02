"""Thin httpx wrapper for Binance public REST endpoints.

Production port of `feasibility/scripts/binance_client.py`. We deliberately
do NOT use ccxt here:
    * adds abstraction we don't need
    * harder to debug rate-limit / pagination edge cases
    * the historical fetch + daily incremental run aren't the place for
      a venue-uniform abstraction; that comes back at L5 (live-trading)

Used by:
    * alpha_factory.data.klines    (1h kline pagination)
    * alpha_factory.data.funding   (8h funding pagination)
    * alpha_factory.data.archive   (orchestrator)

The fetcher in `alpha_factory.data.universe` deliberately does NOT depend
on this — its 4-endpoint snapshot is a one-shot atomic op, and pulling in
the full retry machinery would obscure the atomicity guarantees.
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


class BinanceRateLimitError(BinanceAPIError):
    """Retries exhausted on 418 (IP banned) or 429 (rate-limited).

    Distinct subclass so the orchestrator can ABORT the entire run on a
    rate-limit ban (continuing would extend the ban) while still
    continuing past per-symbol failures of other kinds (geoblock,
    delisted symbol, etc.).
    """


class BinanceClient:
    """Long-lived httpx.Client + retry loop. Use as a context manager.

    The retry loop covers transient network errors AND `RETRYABLE_STATUSES`.
    Honors `Retry-After` if Binance sends one. Non-retryable status codes
    (4xx other than rate-limit) raise `BinanceAPIError` immediately so
    misuse surfaces fast rather than getting absorbed by silent retries.
    """

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
            timeout=httpx.Timeout(
                connect=connect_timeout_s,
                read=read_timeout_s,
                write=connect_timeout_s,
                pool=connect_timeout_s,
            ),
            headers={"User-Agent": "alpha-factory/0.1 (+research)"},
        )

    def __enter__(self) -> BinanceClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._client.close()

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        """GET with retry on transient errors. Raises `BinanceAPIError` on hard fail.

        On retry exhaustion, the FINAL failure mode determines the exception
        type: `BinanceRateLimitError` if the last retried status was 429
        (Binance is also documented to return 418 on hard ban — but 418 is
        non-retryable here, so it raises immediately). Plain
        `BinanceAPIError` for transient-network exhaustion or other 5xx.
        """
        last_err: Exception | None = None
        last_status: int | None = None
        for attempt in range(self.max_retries):
            try:
                r = self._client.get(url, params=params)
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.PoolTimeout,
                httpx.RemoteProtocolError,
            ) as e:
                last_err = e
                wait = self._backoff(attempt)
                log.warning(
                    "transient %s on %s, retrying in %.1fs",
                    type(e).__name__, url, wait,
                )
                time.sleep(wait)
                continue

            status = r.status_code
            if status == 200:
                return r.json()

            if status in NON_RETRYABLE_STATUSES:
                # 418 = IP banned; 451 = geoblock; other 4xx = bad request.
                # 418 is non-retryable here (further requests would extend
                # the ban) — raise BinanceRateLimitError so the orchestrator
                # can abort the whole run, not just continue past one symbol.
                if status == 418:
                    raise BinanceRateLimitError(
                        f"IP banned status=418 url={url} body={r.text[:300]}"
                    )
                raise BinanceAPIError(
                    f"non-retryable status={status} url={url} "
                    f"params={params} body={r.text[:300]}"
                )

            if status in RETRYABLE_STATUSES:
                last_status = status
                # Honor Retry-After if Binance sends one.
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else self._backoff(attempt)
                log.warning(
                    "status=%d on %s, backoff %.1fs (attempt %d/%d)",
                    status, url, wait, attempt + 1, self.max_retries,
                )
                time.sleep(wait)
                continue

            # Unknown status — be paranoid and abort.
            raise BinanceAPIError(
                f"unexpected status={status} url={url} body={r.text[:300]}"
            )

        # Exhausted retries — discriminate the failure mode.
        if last_status == 429:
            raise BinanceRateLimitError(
                f"rate-limit retries exhausted on {url}; last status=429"
            )
        raise BinanceAPIError(
            f"max_retries exhausted on {url}; last error: {last_err}"
        )

    def _backoff(self, attempt: int) -> float:
        return min(self.max_backoff_s, self.base_backoff_s * (2**attempt))
