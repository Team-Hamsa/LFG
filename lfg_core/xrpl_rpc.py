# lfg_core/xrpl_rpc.py
"""Ordered-failover XRPL JSON-RPC clients (#493 follow-up).

Why this exists
---------------
On 2026-09-13 and again 2026-09-14 23:52-00:05 UTC the single configured
public endpoint (s1.ripple.com) answered ``tooBusy`` during bursts of
trait-economy ops. ``xrpl_ops._submit_and_confirm`` raised
``IndeterminateResultError`` — even its confirm-by-hash read hit ``tooBusy`` —
and users lost assets. One busy server must not be able to strand a
transaction, so every backend JSON-RPC call walks an ordered URL list
(``config.JSON_RPC_URLS``, primary first).

Where it hooks in
-----------------
xrpl-py funnels EVERY JSON-RPC exchange through ``Client._request_impl``: the
sync ``JsonRpcClient.request`` is ``asyncio.run(self._request_impl(...))``, and
the library helpers (``submit_and_wait``'s submit + ``ledger``/``tx`` polling,
``autofill``/``autofill_and_sign``'s account/fee/ledger reads, ``simulate``)
call ``client._request_impl`` directly. Overriding ``_request_impl`` therefore
covers all of them with one rule set. The per-URL HTTP exchange below mirrors
``xrpl.asyncio.clients.json_rpc_base.JsonRpcBase._request_impl`` exactly (same
httpx post, same ``json_to_response``, same ``XRPLRequestFailureException`` on
a non-JSON body), so a single-endpoint outcome is byte-for-byte today's.

Failover rules — exact, because this path carries transaction submission
-------------------------------------------------------------------------
Move to the next URL ONLY on:

(a) transport failure — none of these is an answer from the XRP Ledger:

    * ``httpx.RequestError``: connect error, timeout, protocol/network error;
    * an endpoint-level HTTP status (``HTTPStatusFailure``), judged BEFORE the
      body: any 5xx (rippled's plain-text "Server is overloaded" 503, a proxy's
      502/504 page, and also a 5xx carrying a JSON body such as Clio's
      ``503 {"error":"tooBusy"}`` or ``500 {"error":"internal"}`` — xrpl-py's
      ``json_to_response`` ignores the HTTP status, so without this check a 5xx
      JSON body would read as a normal answer), any 1xx/3xx (httpx does not
      follow redirects; a redirecting URL is a misconfigured endpoint), and the
      4xx codes that describe the ENDPOINT rather than the request —
      ``RETRYABLE_HTTP_4XX``: 401/403 (this node refuses us, e.g. an IP-blocked
      or admin-only port), 404 (wrong path on that host), 408 (request
      timeout), 429 (a public node or its proxy rate-limiting our IP). Every
      other 4xx — above all 400, which rippled and Clio use for a genuinely
      malformed request ("params unparseable", "Null method", invalid API
      version) — is a real answer: the identical payload would get the
      identical 400 anywhere. It surfaces immediately with no failover and no
      cooldown, as a single-endpoint client did (JSON body -> ``Response``;
      otherwise ``XRPLRequestFailureException``). rippled answers ordinary RPC
      errors (``txnNotFound``, ``tooBusy``, ...) with HTTP 200 unless the
      request opts into ``ripplerpc`` >= 3.0, which xrpl-py never sends, so
      those are classified by (b), not by status;
    * a 2xx with a non-JSON body (``XRPLRequestFailureException``) or a JSON
      body without the ``result``/``status`` envelope (``KeyError`` — the #385
      malformed-200 shape).

(b) a response whose ``error`` is in ``SOFT_ERROR_CODES`` — the server is
    saying "I can't serve anyone right now", not "your request is wrong":

    * rippled load shedding: ``tooBusy`` (503), ``slowDown`` (429);
    * rippled not synced / not able to trust its ledger (the generic gate in
      ``rpc/detail/Handler.h`` + ``RPCHelpers.cpp``): ``noCurrent``,
      ``noClosed``, ``noNetwork``, ``notSynced``, ``amendmentBlocked``,
      ``unlBlocked`` (expired validator list), ``notReady``;
    * Clio unable to reach its upstream rippled: ``failedToForward``
      (documented universal error) and the codes current Clio actually emits
      for a failed forward (``rpc/Errors.cpp`` ETL errors):
      ``connectionError``, ``requestError``, ``timeout``, ``invalidResponse``.

NEVER fail over on anything else. In particular a transaction engine result
(tes/tec/tef/tel/tem/ter ``engine_result`` in a submit response, or a
validated ``meta.TransactionResult``) is a real answer about the transaction,
and every other ``error`` (``txnNotFound``, ``actNotFound``, ``invalidParams``,
``internal``, ``unknownCmd``, ...) is a real answer about the request — asking a
second server would at best repeat it and at worst mask it. A non-transport
exception (a programming error) propagates immediately.

Why re-sending a submit is safe
-------------------------------
The only write is ``submit`` of an already-SIGNED blob (``SubmitOnly``). This
client re-posts exactly the serialized payload it was given — it never
autofills, never signs, never touches ``Sequence``/``LastLedgerSequence``.
Every endpoint therefore receives the same transaction with the same hash and
Sequence; the ledger applies a given Sequence at most once, so at most one copy
can ever validate. ``xrpl_ops._submit_and_confirm`` signs exactly once before
any submit and hands ``submit_and_wait`` the signed tx with ``wallet=None,
autofill=False``. If the first endpoint actually relayed the tx before failing,
the second answers with a ``tef``/``tel``/``ter`` preliminary result (e.g.
``tefPAST_SEQ``) — not a ``tem``, so ``submit_and_wait`` goes on to poll the
unchanged hash and finds the validated outcome; if that poll raises,
``_confirm_by_hash`` looks the same hash up. A duplicate submit can only
resolve to the one real outcome.

Cooldown
--------
A URL that fails per (a)/(b) is parked for ``COOLDOWN_SECONDS`` (module-level,
monotonic clock, shared by every client in the process) so a burst stops
hammering the busy node: cooling URLs are tried after the available ones. If
every URL is cooling they are all still tried in configured order rather than
failing fast. A success clears that URL's cooldown.

When every URL fails, the LAST outcome surfaces exactly as a single-endpoint
client would have produced it — the soft-error ``Response`` is returned, or the
last transport exception is re-raised as-is (for an endpoint-level HTTP
status: the JSON ``Response`` when the body carried one, else an
``XRPLRequestFailureException`` with the status as ``error``, the shape xrpl-py
raises) — so callers' existing handling
(``is_successful`` checks, ``IndeterminateResultError``, the #385 malformed
retry) behaves unchanged.

Scope: JSON-RPC only. ``XRPL_WS_URL`` / ``XRPL_CLIO_WS_URL`` are not covered.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from json import JSONDecodeError
from typing import Any

import httpx
from xrpl.asyncio.clients import AsyncJsonRpcClient, XRPLRequestFailureException
from xrpl.asyncio.clients.client import REQUEST_TIMEOUT
from xrpl.asyncio.clients.utils import json_to_response, request_to_json_rpc
from xrpl.clients import JsonRpcClient
from xrpl.models.requests.request import Request
from xrpl.models.response import Response, ResponseStatus

logger = logging.getLogger(__name__)

# Server-capacity / not-synced / upstream-unreachable codes. See the module
# docstring for provenance; keep this tight — anything outside it is an answer.
SOFT_ERROR_CODES: frozenset[str] = frozenset(
    {
        # rippled load shedding
        "tooBusy",
        "slowDown",
        # rippled cannot serve from a trustworthy ledger right now
        "noCurrent",
        "noClosed",
        "noNetwork",
        "notSynced",
        "notReady",
        "amendmentBlocked",
        "unlBlocked",
        # Clio could not forward to its rippled
        "failedToForward",
        "connectionError",
        "requestError",
        "timeout",
        "invalidResponse",
    }
)

# 4xx statuses that describe the endpoint, not the request (see docstring).
RETRYABLE_HTTP_4XX: frozenset[int] = frozenset({401, 403, 404, 408, 429})


class HTTPStatusFailure(XRPLRequestFailureException):
    """An endpoint-level HTTP status: fail over. `response` is the parsed JSON
    body when there was one (surfaced if this was the last endpoint)."""

    def __init__(self, status: int, text: str, response: Response | None) -> None:
        super().__init__({"error": status, "error_message": text})
        self.status = status
        self.response = response


class DefinitiveRequestFailure(XRPLRequestFailureException):
    """A request-level HTTP 4xx without a JSON answer: raised as-is, no failover."""


# Transport-level failures: no answer from the ledger was received.
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.RequestError,
    XRPLRequestFailureException,
    KeyError,
)

# Test seam: an httpx transport (e.g. httpx.MockTransport); None = real network.
_http_transport: httpx.AsyncBaseTransport | None = None

COOLDOWN_SECONDS = 45.0

_cooldown_until: dict[str, float] = {}
_cooldown_lock = threading.Lock()


def _monotonic() -> float:
    return time.monotonic()


def reset_cooldowns() -> None:
    """Forget every URL's cooldown (tests / ops)."""
    with _cooldown_lock:
        _cooldown_until.clear()


def _mark_failed(url: str) -> None:
    with _cooldown_lock:
        _cooldown_until[url] = _monotonic() + COOLDOWN_SECONDS


def _mark_ok(url: str) -> None:
    with _cooldown_lock:
        _cooldown_until.pop(url, None)


def _attempt_order(urls: Sequence[str]) -> list[str]:
    """Available URLs in configured order, then cooling ones in configured order."""
    now = _monotonic()
    with _cooldown_lock:
        cooling = {u for u in urls if _cooldown_until.get(u, 0.0) > now}
    return [u for u in urls if u not in cooling] + [u for u in urls if u in cooling]


def _parse_envelope(response: httpx.Response) -> Response | None:
    """The body as an xrpl-py Response, or None if it is not a JSON-RPC envelope."""
    try:
        return json_to_response(response.json())
    except (JSONDecodeError, KeyError, TypeError, AttributeError):
        return None


async def _post_json_rpc(url: str, payload: dict[str, Any], timeout: float) -> Response:
    """One HTTP exchange with one endpoint. A 2xx mirrors
    JsonRpcBase._request_impl exactly; a non-2xx is classified by status first
    (see the module docstring)."""
    async with httpx.AsyncClient(timeout=timeout, transport=_http_transport) as http_client:
        response = await http_client.post(url, json=payload)
        status = response.status_code
        if 200 <= status < 300:
            try:
                return json_to_response(response.json())
            except JSONDecodeError:
                raise XRPLRequestFailureException(
                    {"error": status, "error_message": response.text}
                ) from None
        parsed = _parse_envelope(response)
        if 400 <= status < 500 and status not in RETRYABLE_HTTP_4XX:
            if parsed is not None:
                return parsed
            raise DefinitiveRequestFailure({"error": status, "error_message": response.text})
        raise HTTPStatusFailure(status, response.text, parsed)


def _soft_error(response: Response) -> str | None:
    if response.status != ResponseStatus.ERROR or not isinstance(response.result, dict):
        return None
    code = response.result.get("error")
    return code if isinstance(code, str) and code in SOFT_ERROR_CODES else None


async def failover_request(
    urls: Sequence[str], request: Request, *, timeout: float = REQUEST_TIMEOUT
) -> Response:
    """Send `request` to `urls` in cooldown-aware order per the module rules."""
    # Serialize ONCE, outside the loop: every endpoint gets the identical
    # payload (for a submit, the identical signed blob), and a malformed
    # request model fails immediately instead of cooling down every URL.
    payload = request_to_json_rpc(request)
    method = str(payload.get("method"))
    order = _attempt_order(urls)
    last_response: Response | None = None
    last_exc: BaseException | None = None
    for index, url in enumerate(order):
        try:
            response = await _post_json_rpc(url, payload, timeout)
        except DefinitiveRequestFailure:
            raise
        except HTTPStatusFailure as exc:
            reason = f"HTTP {exc.status}"
            if exc.response is not None:
                last_exc, last_response = None, exc.response
            else:
                last_exc, last_response = exc, None
        except TRANSPORT_ERRORS as exc:
            reason = type(exc).__name__
            last_exc, last_response = exc, None
        else:
            code = _soft_error(response)
            if code is None:
                _mark_ok(url)
                return response
            reason = code
            last_exc, last_response = None, response
        _mark_failed(url)
        if index + 1 < len(order):
            logger.warning(
                "XRPL JSON-RPC %s failed over from %s (%s); trying next endpoint",
                method,
                url,
                reason,
            )
    if last_response is not None:
        return last_response
    assert last_exc is not None
    raise last_exc


def _normalize_urls(urls: Sequence[str]) -> tuple[str, ...]:
    cleaned = tuple(u for u in urls if u)
    if not cleaned:
        raise ValueError("a failover JSON-RPC client needs at least one URL")
    return cleaned


class FailoverJsonRpcClient(JsonRpcClient):
    """Sync ``JsonRpcClient`` that fails over across ``urls`` (module rules).

    ``url`` stays the primary so anything reading it (logs, messages) is
    unchanged."""

    def __init__(self, urls: Sequence[str]) -> None:
        self.urls = _normalize_urls(urls)
        super().__init__(self.urls[0])

    async def _request_impl(
        self, request: Request, *, timeout: float = REQUEST_TIMEOUT
    ) -> Response:
        return await failover_request(self.urls, request, timeout=timeout)


class AsyncFailoverJsonRpcClient(AsyncJsonRpcClient):
    """Async ``AsyncJsonRpcClient`` that fails over across ``urls`` (module rules)."""

    def __init__(self, urls: Sequence[str]) -> None:
        self.urls = _normalize_urls(urls)
        super().__init__(self.urls[0])

    async def _request_impl(
        self, request: Request, *, timeout: float = REQUEST_TIMEOUT
    ) -> Response:
        return await failover_request(self.urls, request, timeout=timeout)
