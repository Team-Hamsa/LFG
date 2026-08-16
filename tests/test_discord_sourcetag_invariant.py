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


def test_admin_burn_stamps_source_tag(monkeypatch):
    # NB: do NOT importlib.reload(admin) — it re-runs @tree.command(name="admin")
    # on the singleton tree (CommandAlreadyRegistered). A plain import is enough:
    # burn_nft reads the module-level SEED/SOURCE_TAG, which every discord test
    # fixture sets to a valid seed before admin is first imported.
    _set_env(monkeypatch)
    import surfaces.discord_bot.admin as admin

    captured = {}

    class _BurnResp:
        result = {"meta": {"TransactionResult": "tesSUCCESS"}}

    def fake_submit(tx, client, wallet):
        captured["tx"] = tx
        return _BurnResp()

    monkeypatch.setattr(admin, "submit_and_wait", fake_submit)
    ok = _run(admin.burn_nft("00080000ABCD"))
    assert ok is True
    assert captured["tx"].source_tag == SOURCE_TAG
