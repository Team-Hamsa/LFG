import asyncio

import lfg_service.app as app
from lfg_core.signing import context


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_defaults_are_xaman_and_no_wallet():
    assert context.current_provider() == "xaman"
    assert context.current_wallet() is None


def test_use_sets_and_resets():
    with context.use("walletconnect", "rWALLET"):
        assert context.current_provider() == "walletconnect"
        assert context.current_wallet() == "rWALLET"
    assert context.current_provider() == "xaman"
    assert context.current_wallet() is None


def test_create_task_inherits_context():
    async def main():
        seen = {}

        async def child():
            seen["p"] = context.current_provider()
            seen["w"] = context.current_wallet()

        with context.use("walletconnect", "rW"):
            t = asyncio.create_task(child())
        await t
        return seen

    assert _run(main()) == {"p": "walletconnect", "w": "rW"}


def test_session_token_round_trips_provider():
    tok = app.make_session_token(
        {"id": "rW", "name": "n", "platform": "web", "provider": "walletconnect"}
    )
    assert app.verify_session_token(tok)["provider"] == "walletconnect"
    tok2 = app.make_session_token({"id": "rW", "name": "n", "platform": "web"})
    assert app.verify_session_token(tok2)["provider"] == "xaman"


class _Req:
    def __init__(self, headers):
        self.headers = headers
        self._s = {}

    def __getitem__(self, k):
        return self._s[k]

    def __setitem__(self, k, v):
        self._s[k] = v


def test_require_auth_sets_context_for_the_handler(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    tok = app.make_session_token(
        {"id": "rW", "name": "n", "platform": "web", "provider": "walletconnect"}
    )
    seen = {}

    @app.require_auth
    async def h(request):
        seen["p"] = context.current_provider()
        seen["w"] = context.current_wallet()
        return app.web.json_response({})

    _run(h(_Req({"Authorization": f"Bearer {tok}"})))
    assert seen == {"p": "walletconnect", "w": "rW"}
    assert context.current_provider() == "xaman"  # reset after the handler
