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
import os

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
