# Canonical Make Waves SourceTag invariant for the Discord surface.
#
# After Spine Plan 3 the bot builds only TWO XRPL/XUMM transactions inline:
#   1. the trustline TrustSet payload (surfaces/discord_bot/trustline.py)
#   2. the admin NFTokenBurn (surfaces/discord_bot/admin.py)
# Everything else (mint / offer / accept) goes through lfg_service, which stamps
# the tag via lfg_core.xrpl_ops / xumm_ops (covered by test_xrpl_source_tag.py +
# test_xumm_source_tag.py). This test guards the two remaining inline paths and
# is where any newly-added inline tx must prove it stamps 2606160021.
import asyncio

from lfg_core.config import SOURCE_TAG

_ENV = {
    "DISCORD_BOT_TOKEN": "t",
    "ADMIN_LOG_CHANNEL_ID": "1",
    "LFG_SERVICE_URL": "http://svc",
    "SERVICE_TOKEN_DISCORD": "s",
    "XUMM_API_KEY": "k",
    "XUMM_API_SECRET": "s",
    "TOKEN_ISSUER_ADDRESS": "rIssuer",
    "TOKEN_CURRENCY_HEX": "ABC",
    "SEED": "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r",
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _set_env(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    import importlib

    import surfaces.discord_bot.config as cfg

    importlib.reload(cfg)


def test_source_tag_constant_is_make_waves():
    assert SOURCE_TAG == 2606160021


def test_trustline_payload_stamps_source_tag(monkeypatch):
    _set_env(monkeypatch)
    import importlib

    import surfaces.discord_bot.trustline as tl

    importlib.reload(tl)

    captured = {}

    class _Resp:
        @staticmethod
        def json():
            return {"refs": {"qr_png": "q"}, "next": {"always": "n"}, "uuid": "u"}

    def fake_post(url, json, headers, timeout=None):
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(tl.requests, "post", fake_post)
    _run(tl.create_trustline_request())
    assert captured["payload"]["txjson"]["TransactionType"] == "TrustSet"
    assert captured["payload"]["txjson"]["SourceTag"] == SOURCE_TAG


def test_admin_burn_stamps_source_tag_and_memos(monkeypatch):
    # NB: do NOT importlib.reload(admin) — it re-runs @tree.command(name="admin")
    # on the singleton tree (CommandAlreadyRegistered). A plain import is enough.
    # The admin burn routes through lfg_core.xrpl_ops.burn_nft, so it carries
    # the SourceTag AND the #54 provenance memos (platform discord-bot).
    _set_env(monkeypatch)
    import surfaces.discord_bot.admin as admin
    from lfg_core import memos, xrpl_ops

    captured = {}

    async def fake_submit_and_confirm(tx, wallet, client, label, **kwargs):
        captured["tx"] = tx
        return {"hash": "B" * 64, "meta": {"TransactionResult": "tesSUCCESS"}}

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit_and_confirm)
    outcome = _run(admin.burn_nft("00080000ABCD"))
    assert outcome.status == admin.BURN_BURNED
    assert outcome.tx_hash == "B" * 64
    tx = captured["tx"]
    assert tx.source_tag == SOURCE_TAG
    assert tx.nftoken_id == "00080000ABCD"
    assert tx.owner is None  # issuer-held tokens only — never a forced burn
    decoded = memos.decode_memos(tx.to_xrpl()["Memos"])
    assert decoded["platform"] == memos.PLATFORM_DISCORD_BOT
    assert decoded["action"] == memos.ACTION_BURN
    assert decoded["initiator"] == memos.INITIATOR_BACKEND


def test_admin_burn_definitive_failure_is_failed(monkeypatch):
    _set_env(monkeypatch)
    import surfaces.discord_bot.admin as admin
    from lfg_core import xrpl_ops

    async def fake_submit_and_confirm(tx, wallet, client, label, **kwargs):
        return None  # validated, definitive failure

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit_and_confirm)
    outcome = _run(admin.burn_nft("00080000ABCD"))
    assert outcome.status == admin.BURN_FAILED
    assert outcome.tx_hash is None


def test_admin_burn_unknown_outcome_is_indeterminate(monkeypatch):
    """A submit whose outcome can't be confirmed is NOT a clean failure: the
    burn may have landed, so the admin must be told, not told it failed."""
    _set_env(monkeypatch)
    import surfaces.discord_bot.admin as admin
    from lfg_core import xrpl_ops

    async def fake_submit_and_confirm(tx, wallet, client, label, **kwargs):
        raise xrpl_ops.IndeterminateResultError("timeout", tx_hash="C" * 64)

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit_and_confirm)
    outcome = _run(admin.burn_nft("00080000ABCD"))
    assert outcome.status == admin.BURN_INDETERMINATE
    assert outcome.tx_hash == "C" * 64


def test_admin_burn_short_circuits_on_simulate_rejection(monkeypatch):
    """#58: a deterministic simulate rejection fails and never submits."""
    _set_env(monkeypatch)
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "1")
    import surfaces.discord_bot.admin as admin
    from lfg_core import xrpl_ops

    def never_submit(*args, **kwargs):
        raise AssertionError("submit_and_wait must not run after a simulate rejection")

    class _SimResp:
        result = {"engine_result": "tecNO_PERMISSION", "applied": False}

    simulated = {}

    def fake_simulate(tx, client, *, binary=False):
        simulated["tx"] = tx
        return _SimResp()

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", never_submit)
    monkeypatch.setattr(xrpl_ops, "simulate", fake_simulate)
    outcome = _run(admin.burn_nft("00080000ABCD"))
    assert outcome.status == admin.BURN_FAILED
    assert simulated["tx"].source_tag == SOURCE_TAG
    assert not simulated["tx"].is_signed()
