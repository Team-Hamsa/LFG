# tests/test_x_poster.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
# verbatim from tests/test_server_identity_wiring.py — same keys/values.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from urllib.parse import unquote  # noqa: E402

import aiohttp  # noqa: E402
import pytest  # noqa: E402

from surfaces.x_bot import x_api  # noqa: E402
from surfaces.x_bot.x_api import XApi, XApiError  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _auth_params(header):
    """Parse an `Authorization: OAuth k1="v1", k2="v2"` header into a dict."""
    assert header.startswith("OAuth ")
    return dict(re.findall(r'([a-zA-Z_]+)="([^"]*)"', header[len("OAuth ") :]))


# ---------------------------------------------------------------------------
# Fake aiohttp layer (no network). XApi only uses session.request(...) as an
# async context manager and reads .status / .headers / await .text().
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status=200, body="{}", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)


def _api(*responses):
    session = _FakeSession(*responses)
    return XApi("ck", "cs", "at", "as", session), session


def _freeze_signing(monkeypatch, nonce="fixednonce", timestamp="1700000000"):
    """Route XApi's signing through the real signer with a frozen nonce and
    timestamp so signatures are deterministic and comparable across calls."""
    real = x_api._signed_headers

    def frozen(method, url, **kwargs):
        kwargs["nonce"] = nonce
        kwargs["timestamp"] = timestamp
        return real(method, url, **kwargs)

    monkeypatch.setattr(x_api, "_signed_headers", frozen)
    return nonce, timestamp


# ---------------------------------------------------------------------------
# T3 — OAuth 1.0a signing (surfaces/x_bot/x_api.py)
# ---------------------------------------------------------------------------
# (a) Published known-good HMAC-SHA1 vectors through our signer wrapper.
# (b) DEFERRED by controller: known-good X API signature fixture requires
#     Task 0's live post (brand credentials not yet provisioned).
# (c) Multipart bodies are EXCLUDED from the signature base string
#     (RFC 5849 §3.4.1.3.1) — a multipart upload signs identically to the
#     same request with no body at all.


def test_oauth_core_photos_vector_reproduces_documented_signature():
    """Published vector: OAuth Core 1.0 spec, Appendix A.5.2 ("photos.example.net",
    https://oauth.net/core/1.0/#AppendixA — the HMAC-SHA1 signing example RFC 5849
    §3.4 is derived from). Fixed nonce/timestamp; documented final signature is
    tR3+Ty81lMeYAr/Fid0kMTYa/WM= over the documented base string
    GET&http%3A%2F%2Fphotos.example.net%2Fphotos&file%3Dvacation.jpg%26oauth_...
    (includes oauth_version="1.0", matching oauthlib's emitted params exactly)."""
    headers = x_api._signed_headers(
        "GET",
        "http://photos.example.net/photos?file=vacation.jpg&size=original",
        consumer_key="dpf43f3p2l4k3l03",
        consumer_secret="kd94hf93k423kf44",
        access_token="nnch734d00sl2jdk",
        access_secret="pfkkdhi9sl3r4s00",
        nonce="kllo9940pd9333jh",
        timestamp="1191242096",
    )
    params = _auth_params(headers["Authorization"])
    assert unquote(params["oauth_signature"]) == "tR3+Ty81lMeYAr/Fid0kMTYa/WM="
    assert params["oauth_signature_method"] == "HMAC-SHA1"
    assert params["oauth_consumer_key"] == "dpf43f3p2l4k3l03"
    assert params["oauth_token"] == "nnch734d00sl2jdk"


# RFC 5849 §3.4.1.1 example base string, as corrected by Errata ID 2550
# (https://www.rfc-editor.org/errata/eid2550): POST http://example.com/request
# with query b5=%3D%253D&a3=a&c%40=&a2=r%20b and form body c2&a3=2 q, client id
# 9djdj82h48djs9d2, token kkk9d7dh3k39sjv7, nonce 7d8f3e4a, timestamp 137131201.
_RFC5849_ERRATA_BASE = (
    "POST&http%3A%2F%2Fexample.com%2Frequest&a2%3Dr%2520b%26a3%3D2%2520q"
    "%26a3%3Da%26b5%3D%253D%25253D%26c%2540%3D%26c2%3D%26oauth_consumer_"
    "key%3D9djdj82h48djs9d2%26oauth_nonce%3D7d8f3e4a%26oauth_signature_m"
    "ethod%3DHMAC-SHA1%26oauth_timestamp%3D137131201%26oauth_token%3Dkkk"
    "9d7dh3k39sjv7"
)


def test_rfc5849_errata_base_string_vector():
    """RFC 5849 §3.4.1.1 example (errata-2550 base string). The RFC does not
    publish the shared secrets (its printed signature is unverifiable — Errata
    ID 4061), so we fix arbitrary secrets and assert our wrapper's signature
    equals HMAC-SHA1 computed over the documented base string with those same
    secrets. oauthlib always emits the OPTIONAL oauth_version="1.0" param (the
    RFC example omits it), so it is spliced into the base string at its sorted
    position; every other byte is the published errata vector. This pins the
    tricky parts: query/body param collection, duplicate keys (a3), space vs
    '+' decoding ("r b", "2 q"), already-encoded values (b5=%3D%253D), and
    that a FORM-ENCODED body DOES enter the base string (contrast with the
    multipart exclusion test below)."""
    base_with_version = _RFC5849_ERRATA_BASE + "%26oauth_version%3D1.0"
    consumer_secret = "j49sk3j29djd"
    token_secret = "dh893hdasih9"
    key = f"{consumer_secret}&{token_secret}"
    expected = base64.b64encode(
        hmac.new(key.encode(), base_with_version.encode(), hashlib.sha1).digest()
    ).decode()

    headers = x_api._signed_headers(
        "POST",
        "http://example.com/request?b5=%3D%253D&a3=a&c%40=&a2=r%20b",
        consumer_key="9djdj82h48djs9d2",
        consumer_secret=consumer_secret,
        access_token="kkk9d7dh3k39sjv7",
        access_secret=token_secret,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        form_body="c2&a3=2+q",  # the RFC shows "c2&a3=2 q"; '+' encodes the space
        nonce="7d8f3e4a",
        timestamp="137131201",
    )
    params = _auth_params(headers["Authorization"])
    assert unquote(params["oauth_signature"]) == expected


def test_multipart_body_excluded_from_signature(monkeypatch):
    """(c) RFC 5849 §3.4.1.3.1: a multipart body contributes NOTHING to the
    signature base string — uploading media must produce the exact same
    Authorization header as signing the bare URL with no body, and no
    multipart field may leak into the OAuth params."""
    _freeze_signing(monkeypatch)
    api, session = _api(
        _FakeResponse(200, json.dumps({"data": {"id": "710", "media_key": "13_710"}}))
    )
    media_id = _run(api.upload_media(b"\x89PNG fake bytes", mime="image/png"))
    assert media_id == "710"

    sent = session.calls[0]
    assert sent["method"] == "POST"
    assert sent["url"] == x_api.MEDIA_UPLOAD_URL
    multipart_auth = sent["headers"]["Authorization"]

    # Same request signed with NO body at all (frozen signer → same nonce/ts).
    bare = x_api._signed_headers(
        "POST",
        x_api.MEDIA_UPLOAD_URL,
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_secret="as",
    )["Authorization"]
    assert multipart_auth == bare

    params = _auth_params(multipart_auth)
    assert set(params) == {
        "oauth_nonce",
        "oauth_timestamp",
        "oauth_version",
        "oauth_signature_method",
        "oauth_consumer_key",
        "oauth_token",
        "oauth_signature",
    }
    # No multipart field name/value in the OAuth params, no draft body-hash.
    assert not {"media", "media_category", "media_type", "oauth_body_hash"} & set(params)
    assert "tweet_image" not in multipart_auth


def test_json_body_excluded_from_signature(monkeypatch):
    """The JSON tweet body is application/json ⇒ excluded from the base string
    (RFC 5849 §3.4.1.3.1): the signature equals the bodyless one and no
    oauth_body_hash param is emitted (oauthlib would add that draft-extension
    param if the body were passed to the signer — X does not support it)."""
    _freeze_signing(monkeypatch)
    api, session = _api(_FakeResponse(201, json.dumps({"data": {"id": "1", "text": "hi"}})))
    _run(api.post_tweet("hi"))

    json_auth = session.calls[0]["headers"]["Authorization"]
    bare = x_api._signed_headers(
        "POST",
        x_api.TWEET_CREATE_URL,
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_secret="as",
    )["Authorization"]
    assert json_auth == bare
    assert "oauth_body_hash" not in _auth_params(json_auth)


# ---------------------------------------------------------------------------
# T3 — request/response shapes and error mapping (mocked aiohttp, no network)
# ---------------------------------------------------------------------------


def test_post_tweet_returns_id_and_sends_json_body():
    api, session = _api(
        _FakeResponse(201, json.dumps({"data": {"id": "1849000000000000001", "text": "gm"}}))
    )
    tweet_id = _run(api.post_tweet("gm"))
    assert tweet_id == "1849000000000000001"

    sent = session.calls[0]
    assert sent["method"] == "POST"
    assert sent["url"] == x_api.TWEET_CREATE_URL
    assert sent["headers"]["Content-Type"] == "application/json"
    assert json.loads(sent["data"]) == {"text": "gm"}


def test_post_tweet_attaches_media_ids():
    api, session = _api(_FakeResponse(201, json.dumps({"data": {"id": "2", "text": "gm"}})))
    _run(api.post_tweet("gm", media_id="710"))
    assert json.loads(session.calls[0]["data"]) == {
        "text": "gm",
        "media": {"media_ids": ["710"]},
    }


def test_upload_media_sends_multipart_form_and_returns_media_id():
    api, session = _api(
        _FakeResponse(
            200,
            json.dumps(
                {
                    "data": {
                        "id": "1146654567674912769",
                        "media_key": "13_1146654567674912769",
                        "expires_after_secs": 86400,
                        "size": 4327,
                    }
                }
            ),
        )
    )
    media_id = _run(api.upload_media(b"pngbytes", mime="image/png"))
    assert media_id == "1146654567674912769"

    data = session.calls[0]["data"]
    assert isinstance(data, aiohttp.FormData)
    fields = {opts["name"]: value for opts, _hdrs, value in data._fields}
    assert fields["media"] == b"pngbytes"
    assert fields["media_category"] == "tweet_image"
    assert fields["media_type"] == "image/png"


def test_verify_credentials_returns_handle():
    api, session = _api(
        _FakeResponse(
            200,
            json.dumps({"data": {"id": "2244994945", "name": "X Dev", "username": "XDevelopers"}}),
        )
    )
    handle = _run(api.verify_credentials())
    assert handle == "XDevelopers"
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == x_api.USERS_ME_URL


def test_429_maps_status_and_earliest_reset():
    api, _ = _api(
        _FakeResponse(
            429,
            json.dumps({"title": "Too Many Requests"}),
            headers={
                "x-rate-limit-reset": "1700000100",
                "x-app-limit-24hour-reset": "1700003600",
                "x-user-limit-24hour-reset": "1700007200",
            },
        )
    )
    with pytest.raises(XApiError) as ei:
        _run(api.post_tweet("gm"))
    err = ei.value
    assert err.status == 429
    assert err.reset_at == 1700000100.0  # earliest applicable reset wins
    assert "Too Many Requests" in err.body


def test_429_without_reset_headers_has_none_reset_at():
    api, _ = _api(_FakeResponse(429, "slow down", headers={"x-rate-limit-reset": "not-a-number"}))
    with pytest.raises(XApiError) as ei:
        _run(api.post_tweet("gm"))
    assert ei.value.status == 429
    assert ei.value.reset_at is None


def test_500_maps_status_and_truncates_body():
    api, _ = _api(_FakeResponse(500, "<html>" + "x" * 5000 + "</html>"))
    with pytest.raises(XApiError) as ei:
        _run(api.upload_media(b"png"))
    err = ei.value
    assert err.status == 500
    assert err.reset_at is None
    assert len(err.body) <= x_api._BODY_LIMIT


def test_unexpected_success_shape_raises_xapierror():
    # 200 but no data.id — must raise (callers own retries; they need a
    # status to distinguish, not a KeyError).
    api, _ = _api(_FakeResponse(200, json.dumps({"detail": "weird"})))
    with pytest.raises(XApiError) as ei:
        _run(api.post_tweet("gm"))
    assert ei.value.status == 200
