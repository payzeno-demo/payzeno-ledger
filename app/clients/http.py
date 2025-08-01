"""Thin httpx wrapper shared by every outbound acquirer client.

Owns exactly three things: timeouts, error translation, and the structured log line.
Nothing above this module ever sees an ``httpx`` exception — by the time a failure
leaves here it is already a subclass of :class:`app.errors.UpstreamError`, which is
what lets ``SettlementPoster`` branch on ``exc.code`` instead of on transport types.

There is deliberately **no** retry loop in this class. Retries on the settlement path
are a business decision (``reconciliation_item.next_attempt_at``, backoff, attempt
count) and belong to ``RetryScheduler``; a silent transport retry here would multiply
every acquirer call by three during exactly the outage where that is most harmful.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

import httpx

from app.errors import (
    ProcessorIndeterminateError,
    ProcessorUnavailableError,
    UpstreamError,
)
from app.logging import get_logger

logger = get_logger(__name__)

#: Upstream statuses that mean "the acquirer is unwell", as opposed to "we sent junk".
_UNAVAILABLE_STATUSES: frozenset[int] = frozenset({500, 502, 503, 504})


class LedgerHttpxClient:
    """One pooled ``httpx.AsyncClient`` per acquirer.

    :param base_url: ``WORLDFLOW_BASE_URL`` or ``NORDPAY_BASE_URL``.
    :param api_key: bearer token presented on every request.
    :param acquirer: the acquirer slug, used for logging and error details.
    :param acquirer_account: value of the ``Payzeno-Acquirer-Account`` header.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        acquirer: str,
        acquirer_account: str,
        connect_timeout_seconds: float = 2.0,
        read_timeout_seconds: float = 8.0,
    ) -> None:
        self._acquirer = acquirer
        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=read_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=self._timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Payzeno-Acquirer-Account": acquirer_account,
                "Accept": "application/json",
                "User-Agent": "payzeno-ledger/1.31",
            },
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

    @property
    def acquirer(self) -> str:
        return self._acquirer

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        return await self._request("GET", path, params=params, headers=headers)

    async def post(
        self,
        path: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        return await self._request("POST", path, json=json, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        started = time.monotonic()
        try:
            response = await self._client.request(
                method, path, params=params, json=json, headers=dict(headers or {})
            )
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            # We sent the request and never learned the outcome. For a capture this is
            # the one state where the cardholder may or may not have been charged.
            self._log(method, path, None, started, "timeout")
            raise ProcessorIndeterminateError(
                f"{self._acquirer} timed out on {method} {path}",
                code="processor_timeout",
                acquirer=self._acquirer,
                path=path,
            ) from exc
        except httpx.ConnectError as exc:
            self._log(method, path, None, started, "connect_error")
            raise ProcessorUnavailableError(
                f"{self._acquirer} unreachable",
                code="processor_unavailable",
                acquirer=self._acquirer,
                path=path,
            ) from exc
        except httpx.RemoteProtocolError as exc:
            self._log(method, path, None, started, "connection_reset")
            raise ProcessorIndeterminateError(
                f"{self._acquirer} reset the connection on {method} {path}",
                code="processor_connection_reset",
                acquirer=self._acquirer,
                path=path,
            ) from exc

        self._log(method, path, response.status_code, started, "ok")
        self._raise_for_status(method, path, response)
        return response

    def _raise_for_status(self, method: str, path: str, response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        if status == 429:
            raise ProcessorUnavailableError(
                f"{self._acquirer} rate limited {method} {path}",
                code="rate_limited",
                acquirer=self._acquirer,
                status=status,
            )
        if status in _UNAVAILABLE_STATUSES:
            raise ProcessorUnavailableError(
                f"{self._acquirer} returned {status} for {method} {path}",
                code="processor_unavailable",
                acquirer=self._acquirer,
                status=status,
                body=_safe_body(response),
            )
        raise UpstreamError(
            f"{self._acquirer} rejected {method} {path} with {status}",
            code="internal_error",
            acquirer=self._acquirer,
            status=status,
            body=_safe_body(response),
        )

    def _log(
        self,
        method: str,
        path: str,
        status: int | None,
        started: float,
        outcome: str,
    ) -> None:
        logger.info(
            "acquirer_request",
            acquirer=self._acquirer,
            method=method,
            path=path,
            status=status,
            outcome=outcome,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def _safe_body(response: httpx.Response) -> str:
    """Never let an acquirer body of unknown size into an exception detail dict."""
    try:
        return response.text[:512]
    except (UnicodeDecodeError, httpx.ResponseNotRead):  # pragma: no cover - defensive
        return "<unreadable>"
