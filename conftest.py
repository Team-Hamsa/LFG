# conftest.py — repo-root pytest env guard.
# lfg_core/config.py freezes constants from the environment (via load_dotenv)
# at first import, and the machine's .env is the LIVE deployment config — e.g.
# it sets ECONOMY_ENABLED=0 after the mainnet cutover (#113), which broke the
# tests that assert the enabled default. pytest imports this file before any
# test module, and load_dotenv() never overrides an already-set variable, so
# setdefault here pins the test default suite-wide. Explicit shell exports
# still win (setdefault), so a run can force a value when needed.
#
# config.validate_economy_config now refuses to import when ECONOMY_ENABLED is
# on while ECONOMY_NETWORK != XRPL_NETWORK (go-live review B5). The machine
# .env is XRPL_NETWORK=mainnet, and forcing the economy on with the default
# testnet ECONOMY_NETWORK would be exactly that illegal split — so pin both
# networks to testnet here too, giving the suite a coherent enabled+matching
# posture. (setdefault, so explicit shell exports still win.)
import asyncio
import os
import warnings

# Hermetic throwaway values for modules that import the shared config during
# fixture setup. Individual config tests can still override them explicitly;
# setdefault guarantees a developer/deployment environment always wins.
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault(
    "TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000"
)
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("ECONOMY_ENABLED", "1")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("ECONOMY_NETWORK", "testnet")
# Payload creates spawn a XUMM websocket watcher task; tests must never open
# real sockets (and short-lived loops would leak pending tasks). The status
# cache's freshness window would likewise make repeated same-uuid polls in a
# test serve stale state, so disable the throttle (terminal-state caching
# remains; the fixture below clears it between tests).
os.environ.setdefault("XUMM_WS_WATCH", "0")
os.environ.setdefault("XUMM_STATUS_CACHE_SECONDS", "0")
os.environ.setdefault("XRPL_ACTIONS_BATCH_ENABLED", "0")


from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

_LEGACY_TEST_LOOPS: list[asyncio.AbstractEventLoop] = []


@pytest.fixture(autouse=True)
def _legacy_sync_test_event_loop(request: pytest.FixtureRequest) -> None:
    """Keep pre-asyncio.run sync tests working after pytest async tests.

    Python 3.13 raises from get_event_loop() once an isolated pytest-asyncio
    loop has been closed. A number of older synchronous tests intentionally
    drive one coroutine with get_event_loop().run_until_complete(). Give only
    those unmarked sync tests a current loop; pytest-asyncio continues to own
    every @pytest.mark.asyncio test loop.
    """

    if request.node.get_closest_marker("asyncio") is not None:
        return
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _LEGACY_TEST_LOOPS.append(loop)


@pytest.fixture(scope="session", autouse=True)
def _close_legacy_sync_test_event_loops() -> None:
    yield
    for loop in _LEGACY_TEST_LOOPS:
        if not loop.is_closed():
            loop.close()


@pytest.fixture(autouse=True)
def _reset_xumm_status_cache() -> None:
    # get_payload_status caches per-uuid results (terminal ones forever) and
    # 429s arm a global cooldown — both module-level, so scrub between tests.
    from lfg_core import xumm_ops

    xumm_ops._STATUS_CACHE.clear()
    xumm_ops._watched.clear()
    xumm_ops._rate_limited_until = 0.0
    # The service's per-user sign-in creation limiter is module state too.
    import sys

    app_mod = sys.modules.get("lfg_service.app")
    if app_mod is not None:
        app_mod._signin_create_hits.clear()


@pytest.fixture(autouse=True)
def _isolated_payment_ledger(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # wait_for_payment now records consumed payments (issue #196); point the
    # ledger at a per-test file so tests never write the real app DB and a
    # tx hash consumed by one test can't fail the next.
    from lfg_core import payment_ledger

    monkeypatch.setattr(payment_ledger, "_db_path", lambda: str(tmp_path / "payment_ledger.db"))
