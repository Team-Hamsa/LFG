# The signing seam (#399): SourceTag + provenance memos are enforced by the
# interface, not by each implementation remembering to do it.
#
# Before this, "every transaction carries SourceTag 2606160021" held because
# there happened to be one door (xumm_ops._create_xumm_payload) plus one
# hand-rolled copy in the Discord trustline builder. That is a property of the
# codebase's shape, not an invariant — and #399 adds a second signer whose
# payload is relayed through the USER'S BROWSER, where stamping on the way out
# guarantees nothing about what comes back. Hence stamp AND validate.

import asyncio

import pytest

from lfg_core import config, memos, signing
from lfg_core.signing import provenance
from lfg_core.signing.base import BaseSigningProvider
from lfg_core.signing.types import SignHandle, SignRequest, SignStatus
from lfg_core.signing.xaman import XamanProvider


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _memos():
    return memos.build_memos_json(
        memos.INITIATOR_USER, memos.PLATFORM_DISCORD_ACTIVITY, memos.ACTION_MINT
    )


# --- stamping keeps its existing semantics exactly --------------------------


def test_stamp_applies_the_source_tag_and_memos():
    tx = {"TransactionType": "Payment"}
    provenance.stamp(tx, _memos())
    assert tx["SourceTag"] == config.SOURCE_TAG
    assert memos.decode_memos(tx["Memos"])["action"] == memos.ACTION_MINT


def test_stamp_never_overwrites_a_caller_supplied_value():
    """setdefault, not assignment — pinned because switching to assignment
    would silently change behaviour for every builder that pre-sets a field."""
    tx = {"TransactionType": "Payment", "SourceTag": config.SOURCE_TAG, "Memos": []}
    provenance.stamp(tx, _memos())
    assert tx["Memos"] == []


def test_signin_is_exempt_from_both():
    tx = {"TransactionType": "SignIn"}
    provenance.stamp_and_validate(tx, _memos())
    assert "SourceTag" not in tx
    assert "Memos" not in tx


# --- validation is what a second provider actually needs --------------------


def test_a_stripped_source_tag_is_rejected():
    """The WalletConnect case: the client relays txjson to the wallet, so it
    can strip the tag before the wallet ever sees it. Stamping cannot catch
    that; validating the returned transaction can."""
    tx = {"TransactionType": "Payment", "Memos": _memos()}
    with pytest.raises(provenance.ProvenanceError, match="SourceTag"):
        provenance.validate(tx)


def test_a_wrong_source_tag_is_rejected():
    tx = {"TransactionType": "Payment", "SourceTag": 12345, "Memos": _memos()}
    with pytest.raises(provenance.ProvenanceError, match="Make Waves"):
        provenance.validate(tx)


def test_malformed_memos_are_rejected():
    tx = {"TransactionType": "Payment", "SourceTag": config.SOURCE_TAG, "Memos": ["nope"]}
    with pytest.raises(provenance.ProvenanceError, match="malformed"):
        provenance.validate(tx)


def test_memos_outside_the_closed_enum_are_rejected():
    from xrpl.utils import str_to_hex

    forged = [
        {"Memo": {"MemoType": str_to_hex(k), "MemoData": str_to_hex(v)}}
        for k, v in (("initiator", "user"), ("platform", "myspace"), ("action", "mint"))
    ]
    tx = {"TransactionType": "Payment", "SourceTag": config.SOURCE_TAG, "Memos": forged}
    with pytest.raises(provenance.ProvenanceError, match="closed enum"):
        provenance.validate(tx)


def test_missing_memo_keys_are_rejected():
    from xrpl.utils import str_to_hex

    partial = [{"Memo": {"MemoType": str_to_hex("initiator"), "MemoData": str_to_hex("user")}}]
    tx = {"TransactionType": "Payment", "SourceTag": config.SOURCE_TAG, "Memos": partial}
    with pytest.raises(provenance.ProvenanceError, match="platform, action"):
        provenance.validate(tx)


def test_memos_are_optional_by_default_but_the_source_tag_is_not():
    """The CLI economy drivers legitimately have no surface to attribute to."""
    tx = {"TransactionType": "Payment"}
    provenance.stamp_and_validate(tx, None)  # no raise
    assert tx["SourceTag"] == config.SOURCE_TAG
    with pytest.raises(provenance.ProvenanceError, match="no provenance Memos"):
        provenance.validate(tx, require_memos=True)


def test_a_signin_carrying_a_source_tag_is_rejected():
    tx = {"TransactionType": "SignIn", "SourceTag": config.SOURCE_TAG}
    with pytest.raises(provenance.ProvenanceError, match="pseudo-transaction"):
        provenance.validate(tx)


# --- the base class makes it non-optional -----------------------------------


class _Recorder(BaseSigningProvider):
    name = "recorder"

    def __init__(self):
        self.seen = []

    async def _create(self, request):
        self.seen.append(request.txjson)
        return SignHandle(id="h1", raw={})

    async def status(self, handle_id):
        return SignStatus(signed=True, resolved=True)

    async def cancel(self, handle_id):
        return True


def test_a_provider_cannot_receive_an_unstamped_transaction():
    """The whole point: an implementation never sees a txjson it could forget
    to stamp — create() is final and does it first."""
    p = _Recorder()
    _run(p.create(SignRequest(txjson={"TransactionType": "Payment"}, memos_json=_memos())))
    assert p.seen[0]["SourceTag"] == config.SOURCE_TAG
    assert memos.decode_memos(p.seen[0]["Memos"])["platform"] == memos.PLATFORM_DISCORD_ACTIVITY


def test_create_does_not_mutate_the_callers_dict():
    p = _Recorder()
    original = {"TransactionType": "Payment"}
    _run(p.create(SignRequest(txjson=original, memos_json=_memos())))
    assert original == {"TransactionType": "Payment"}


def test_a_provider_demanding_memos_refuses_a_bare_transaction():
    class _Strict(_Recorder):
        require_memos = True

    p = _Strict()
    with pytest.raises(provenance.ProvenanceError):
        _run(p.create(SignRequest(txjson={"TransactionType": "Payment"})))
    assert p.seen == []


# --- the Xaman provider is a faithful adapter, not a rewrite ----------------


def test_xaman_provider_delegates_to_xumm_ops(monkeypatch):
    captured = {}

    async def _fake(txjson, options=None, user_token=None, memos_json=None):
        captured.update(
            txjson=txjson, options=options, user_token=user_token, memos_json=memos_json
        )
        return {
            "uuid": "u-1",
            "xumm_url": "https://xumm.app/sign/u-1",
            "qr_url": "https://xumm.app/q.png",
            "push": "sent",
            "pushed": True,
        }

    monkeypatch.setattr("lfg_core.xumm_ops._create_xumm_payload", _fake)

    handle = _run(
        XamanProvider().create(
            SignRequest(
                txjson={"TransactionType": "Payment"},
                memos_json=_memos(),
                options={"expire": 15},
                user_token="tok",
            )
        )
    )
    assert handle.id == "u-1"
    assert handle.sign_url == "https://xumm.app/sign/u-1"
    assert handle.push == "sent"
    # The provider-native payload survives verbatim, so existing call sites
    # reading qr_url/xumm_url off it are unaffected.
    assert handle.raw["pushed"] is True
    assert captured["user_token"] == "tok"
    assert captured["options"] == {"expire": 15}
    assert captured["txjson"]["SourceTag"] == config.SOURCE_TAG


def test_xaman_provider_returns_none_when_the_payload_is_not_created(monkeypatch):
    async def _fake(*a, **k):
        return None

    monkeypatch.setattr("lfg_core.xumm_ops._create_xumm_payload", _fake)
    assert _run(XamanProvider().create(SignRequest(txjson={"TransactionType": "Payment"}))) is None


def test_a_failed_status_lookup_is_not_a_decline(monkeypatch):
    """Three-way `signed`: None means 'we do not know', never 'the user said
    no'. Reading a transient XUMM error as a decline would fail live mints."""

    async def _fake(_uuid):
        return None

    monkeypatch.setattr("lfg_core.xumm_ops.get_payload_status", _fake)
    status = _run(XamanProvider().status("u-1"))
    assert status.signed is None
    assert status.resolved is False


def test_status_maps_a_signed_payload(monkeypatch):
    async def _fake(_uuid):
        return {
            "signed": True,
            "resolved": True,
            "txid": "ABC",
            "signer": "rSigner",
            "user_token": "tok",
        }

    monkeypatch.setattr("lfg_core.xumm_ops.get_payload_status", _fake)
    status = _run(XamanProvider().status("u-1"))
    assert (status.signed, status.txid, status.signer, status.user_token) == (
        True,
        "ABC",
        "rSigner",
        "tok",
    )


# --- registry ---------------------------------------------------------------


def test_default_provider_is_xaman():
    assert signing.get_provider().name == "xaman"
    assert isinstance(signing.get_provider("XAMAN"), XamanProvider)


def test_provider_instances_are_cached():
    assert signing.get_provider("xaman") is signing.get_provider("xaman")


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown signing provider"):
        signing.get_provider("metamask")


def test_xaman_provider_satisfies_the_protocol():
    from lfg_core.signing.base import SigningProvider

    assert isinstance(XamanProvider(), SigningProvider)
