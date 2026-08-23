# conftest.py — repo-root pytest env guard.
# lfg_core/config.py freezes constants from the environment at first import,
# and the machine's .env is the LIVE deployment config. Since #323 the suite
# SKIPS that .env entirely (LFG_SKIP_DOTENV=1 below gates config's
# load_dotenv()), so the pins here — applied before any test module imports
# lfg_core — are the suite's environment. Explicit shell exports still win
# (setdefault), so a run can force a value when needed.
#
# config.validate_economy_config now refuses to import when ECONOMY_ENABLED is
# on while ECONOMY_NETWORK != XRPL_NETWORK (go-live review B5). The machine
# .env is XRPL_NETWORK=mainnet, and forcing the economy on with the default
# testnet ECONOMY_NETWORK would be exactly that illegal split — so pin both
# networks to testnet here too, giving the suite a coherent enabled+matching
# posture. (setdefault, so explicit shell exports still win.)
import os

# --- Isolate the suite from the deployed .env (#323) ---
# lfg_core/config.py gates its load_dotenv() on LFG_SKIP_DOTENV, so with this
# set the box's live .env FILE never reaches a test. Everything below is a
# FALLBACK pin (setdefault): it fills the value only when the shell didn't —
# an explicit export deliberately still wins, the documented escape hatch
# (e.g. `XRPL_NETWORK=mainnet pytest -k …`). So the suite runs against these
# fallbacks plus whatever the invoker explicitly exported — NOT a fully fixed
# environment. Because the .env no longer supplies them, the
# _require(...)-mandatory vars and layer knobs must be pinned here centrally
# (they used to arrive via per-file env-guard preambles racing the .env;
# those preambles are now harmless no-ops).
os.environ.setdefault("LFG_SKIP_DOTENV", "1")
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway testnet seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

os.environ.setdefault("ECONOMY_ENABLED", "1")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("ECONOMY_NETWORK", "testnet")
# Payload creates spawn a XUMM websocket watcher task; tests must never open
# real sockets (and short-lived loops would leak pending tasks). The status
# cache's freshness window would likewise make repeated same-uuid polls in a
# test serve stale state, so disable the throttle (terminal-state caching
# remains; the fixture below clears it between tests).
# Pre-submit simulate pre-flight (#58): the existing _submit_and_confirm /
# sponsored-prepare suites stub autofill_and_sign / submit_and_wait, which
# does NOT intercept a `simulate` call, so with the flag on they would hit the
# network. Pin it off; tests that exercise the pre-flight force it on with
# monkeypatch.setenv (the helper reads the flag at call time).
os.environ.setdefault("PRESUBMIT_SIMULATE", "0")
os.environ.setdefault("XUMM_WS_WATCH", "0")
os.environ.setdefault("XUMM_STATUS_CACHE_SECONDS", "0")
# Same hazard, tuned knobs: test_bulk_mint_ui_flag / test_shop_config /
# test_shop_pricing assert the DEFAULT each of these falls back to when unset,
# but they read the frozen config constant — so the machine .env's live values
# (BULK_MINT_UI_ENABLED=1 since the flag went on, SHOP_* retuned for mainnet
# pricing) fail them for no real reason. Pin the documented defaults, matching
# lfg_core/config.py. Every push from a checkout under the deployment tree runs
# these through the pre-push pytest gate; CI passes only because the runner has
# no .env at all.
os.environ.setdefault("BULK_MINT_UI_ENABLED", "0")
os.environ.setdefault("SHOP_BASE_BRIX", "1.0")
os.environ.setdefault("SHOP_MIN_BRIX", "5")
os.environ.setdefault("SHOP_MAX_BRIX", "5000")
os.environ.setdefault("SHOP_OFFER_TTL_SECONDS", "900")


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
