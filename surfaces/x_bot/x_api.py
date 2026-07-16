"""OAuth 1.0a-signed X (Twitter) API client (#41).

Thin, deliberately retry-free wrapper over three endpoints: create tweet
(v2), upload media (v2 — v1.1 upload.twitter.com was retired 2025-06-09),
and verify credentials (v2 users/me). Callers (bot.py) own all retry and
backoff policy; every failure surfaces as :class:`XApiError`, which carries
enough (``status``, ``body``, ``reset_at``) to distinguish 4xx vs 5xx vs 429
and honor rate-limit resets.

Signing goes through ``oauthlib.oauth1.Client`` (HMAC-SHA1, Authorization
header placement) — the spec explicitly rejects hand-rolling RFC 5849.
Body handling (RFC 5849 §3.4.1.3.1): only ``application/x-www-form-urlencoded``
bodies participate in the signature base string. JSON and multipart bodies
are therefore never passed to the signer — the request is signed as if it
had no body, and the payload is attached only to the aiohttp request. (This
also avoids oauthlib 3.3+'s draft ``oauth_body_hash`` extension param, which
it would add for non-form bodies and which X does not support; it matches
requests-oauthlib's proven behavior against X.)

No XRPL transactions here — no SourceTag surface.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp
from oauthlib.oauth1 import SIGNATURE_HMAC_SHA1, SIGNATURE_TYPE_AUTH_HEADER, Client

_HTTPMethod = Literal["GET", "POST"]

TWEET_CREATE_URL = "https://api.x.com/2/tweets"
USERS_ME_URL = "https://api.x.com/2/users/me"
MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"

# Response body chars kept on XApiError (X error JSON is small; HTML error
# pages are not worth carrying whole).
_BODY_LIMIT = 1000

# Rate-limit reset header families (epoch seconds). The 15-minute-window set
# is documented on docs.x.com/x-api/fundamentals/rate-limits; the 24-hour
# app/user pools are observed on write endpoints. All optional — read
# defensively, use the earliest applicable reset.
_RESET_HEADERS = (
    "x-rate-limit-reset",
    "x-app-limit-24hour-reset",
    "x-user-limit-24hour-reset",
)


class XApiError(Exception):
    """X API failure.

    - ``status``: HTTP status (429 = rate limited; a 2xx status means the
      response body didn't have the expected shape).
    - ``body``: response body, truncated to ``_BODY_LIMIT`` chars.
    - ``reset_at``: epoch seconds of the earliest rate-limit reset header
      present on the response, or ``None``.
    """

    def __init__(self, status: int, body: str, reset_at: float | None = None) -> None:
        self.status = status
        self.body = body[:_BODY_LIMIT]
        self.reset_at = reset_at
        message = f"X API error {status}"
        if reset_at is not None:
            message += f" (rate limit resets at {reset_at:.0f})"
        if self.body:
            message += f": {self.body[:200]}"
        super().__init__(message)


def _signed_headers(
    method: _HTTPMethod,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    access_secret: str,
    headers: Mapping[str, str] | None = None,
    form_body: str | None = None,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> dict[str, str]:
    """Build OAuth 1.0a-signed headers for a request via oauthlib.

    ``form_body`` must only be a ``application/x-www-form-urlencoded`` body —
    the one body kind RFC 5849 §3.4.1.3.1 includes in the signature base
    string. JSON/multipart callers pass no body here (signed as bodyless).
    ``nonce``/``timestamp`` override oauthlib's generated values (tests only).
    """
    client = Client(
        consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=access_token,
        resource_owner_secret=access_secret,
        signature_method=SIGNATURE_HMAC_SHA1,
        signature_type=SIGNATURE_TYPE_AUTH_HEADER,
        nonce=nonce,
        timestamp=timestamp,
    )
    _, signed, _ = client.sign(url, http_method=method, body=form_body, headers=dict(headers or {}))
    return {str(k): str(v) for k, v in signed.items()}


def _media_upload_request(image_bytes: bytes, mime: str) -> tuple[str, aiohttp.FormData]:
    """The one place that knows the media-upload endpoint + request shape.

    Currently the v2 single-request endpoint (images don't need the chunked
    INIT/APPEND/FINALIZE flow). Switching endpoint or shape (e.g. back to a
    v1.1-style host, or to the split /2/media/upload/initialize family) is a
    change to this function only.
    """
    form = aiohttp.FormData()
    form.add_field("media", image_bytes, filename="media", content_type=mime)
    form.add_field("media_category", "tweet_image")
    form.add_field("media_type", mime)
    return MEDIA_UPLOAD_URL, form


def _earliest_reset(headers: Mapping[str, str]) -> float | None:
    """Earliest epoch-seconds reset across the rate-limit header families."""
    resets: list[float] = []
    for name in _RESET_HEADERS:
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            resets.append(float(raw))
        except ValueError:
            continue
    return min(resets) if resets else None


@dataclass
class _ApiResponse:
    status: int
    body: str


def _data_str(resp: _ApiResponse, key: str) -> str:
    """Extract ``data.<key>`` (a string) from a v2 response body."""
    try:
        value = json.loads(resp.body)["data"][key]
    except (ValueError, KeyError, TypeError) as exc:
        raise XApiError(resp.status, resp.body) from exc
    if not isinstance(value, str):
        raise XApiError(resp.status, resp.body)
    return value


class XApi:
    """OAuth 1.0a user-context client for the X API v2 (retry-free)."""

    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        access_token: str,
        access_secret: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._access_token = access_token
        self._access_secret = access_secret
        self._session = session

    async def verify_credentials(self) -> str:
        """Return the authenticated account's handle (``data.username``)."""
        resp = await self._send("GET", USERS_ME_URL)
        return _data_str(resp, "username")

    async def post_tweet(self, text: str, media_id: str | None = None) -> str:
        """Create a tweet; return its id. JSON body is excluded from the
        signature base string (see module docstring)."""
        payload: dict[str, Any] = {"text": text}
        if media_id is not None:
            payload["media"] = {"media_ids": [media_id]}
        resp = await self._send(
            "POST",
            TWEET_CREATE_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
        )
        return _data_str(resp, "id")

    async def upload_media(self, image_bytes: bytes, mime: str = "image/png") -> str:
        """Upload one image; return the media_id (``data.id``). The multipart
        body is EXCLUDED from the signature base string (RFC 5849
        §3.4.1.3.1): the request is signed bare, the payload rides only on
        the aiohttp request."""
        url, form = _media_upload_request(image_bytes, mime)
        resp = await self._send("POST", url, data=form)
        return _data_str(resp, "id")

    def _sign(
        self, method: _HTTPMethod, url: str, headers: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        return _signed_headers(
            method,
            url,
            consumer_key=self._consumer_key,
            consumer_secret=self._consumer_secret,
            access_token=self._access_token,
            access_secret=self._access_secret,
            headers=headers,
        )

    async def _send(
        self,
        method: _HTTPMethod,
        url: str,
        headers: Mapping[str, str] | None = None,
        data: Any = None,
    ) -> _ApiResponse:
        signed = self._sign(method, url, headers)
        async with self._session.request(method, url, headers=signed, data=data) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise XApiError(resp.status, text, reset_at=_earliest_reset(resp.headers))
            return _ApiResponse(resp.status, text)
