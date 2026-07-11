# Config invariant for the trait economy (go-live review B5):
#   - ECONOMY_ENABLED now defaults OFF (opt-in).
#   - When ECONOMY_ENABLED is on, ECONOMY_NETWORK MUST equal XRPL_NETWORK, or
#     the process refuses to boot. The economy's DB/gates resolve on
#     ECONOMY_NETWORK while its irreversible on-ledger ops sign against
#     XRPL_NETWORK; a split would land asset ops on the wrong chain.
#
# The pure-function checks run in-process; the "does config refuse to import"
# and "what is the real default" checks run in a clean subprocess so no other
# test module's env pollution (e.g. test_snapshot_balances sets ECONOMY_ENABLED
# via setdefault) can affect the result.

import os
import subprocess
import sys

# Env guard: set before lfg_core imports so frozen config constants are sane
# when this file runs first (see test-env-guard convention).
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import config  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- Pure-function behavior (in-process, deterministic) ---------------------


def test_validate_raises_when_enabled_and_networks_mismatch():
    try:
        config.validate_economy_config(True, "testnet", "mainnet")
    except ValueError as e:
        # Message must name both offending vars so an operator can fix it fast.
        assert "ECONOMY_NETWORK" in str(e)
        assert "XRPL_NETWORK" in str(e)
    else:
        raise AssertionError("expected ValueError on enabled + mismatched networks")


def test_validate_passes_when_enabled_and_networks_match():
    # No raise == pass.
    config.validate_economy_config(True, "mainnet", "mainnet")
    config.validate_economy_config(True, "testnet", "testnet")


def test_validate_skips_when_disabled_even_if_mismatched():
    # Disabled: the split-network hazard is inert, so the check is a no-op.
    config.validate_economy_config(False, "testnet", "mainnet")


# --- Import-time behavior in a clean subprocess -----------------------------


def _config_subprocess(tmp_path, extra_env, code):
    """Import lfg_core.config in a scrubbed env from a secretless cwd.

    Runs from tmp_path (outside the repo) so python-dotenv finds no real .env,
    and builds env from scratch so no inherited ECONOMY_* leaks in.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": REPO_ROOT,
        "XUMM_API_KEY": "test",
        "XUMM_API_SECRET": "test",
        "SEED": "sEdTM1uX8pu2do5XvTnutH6HsouMaM2",
        "TOKEN_ISSUER_ADDRESS": "rrrrrrrrrrrrrrrrrrrrrhoLvTp",
        "TOKEN_CURRENCY_HEX": "4C46474F00000000000000000000000000000000",
        "BUNNY_CDN_ACCESS_KEY": "test",
        "BUNNY_CDN_STORAGE_ZONE": "test",
        "LAYER_SOURCE": "local",
        "BUNNY_PULL_ZONE": "nft.pullzone.example",
    }
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def test_economy_enabled_defaults_off(tmp_path):
    # Authoritative default check: no ECONOMY_ENABLED in env at all.
    result = _config_subprocess(
        tmp_path,
        {"XRPL_NETWORK": "testnet"},
        "import lfg_core.config as c; print(c.ECONOMY_ENABLED)",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_boot_refused_when_enabled_and_networks_mismatch(tmp_path):
    result = _config_subprocess(
        tmp_path,
        {
            "ECONOMY_ENABLED": "1",
            "XRPL_NETWORK": "mainnet",
            "ECONOMY_NETWORK": "testnet",
        },
        "import lfg_core.config",
    )
    assert result.returncode != 0
    assert "ECONOMY_NETWORK" in result.stderr and "XRPL_NETWORK" in result.stderr


def test_boot_ok_when_enabled_and_networks_match(tmp_path):
    result = _config_subprocess(
        tmp_path,
        {
            "ECONOMY_ENABLED": "1",
            "XRPL_NETWORK": "mainnet",
            "ECONOMY_NETWORK": "mainnet",
        },
        "import lfg_core.config as c; print(c.ECONOMY_ENABLED)",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_boot_ok_when_disabled_and_networks_mismatch(tmp_path):
    # The disable is a valid deployment posture (economy still testnet-gated
    # while the app runs on mainnet); a mismatch must NOT block boot then.
    result = _config_subprocess(
        tmp_path,
        {
            "ECONOMY_ENABLED": "0",
            "XRPL_NETWORK": "mainnet",
            "ECONOMY_NETWORK": "testnet",
        },
        "import lfg_core.config",
    )
    assert result.returncode == 0, result.stderr


def test_economy_network_is_normalized(tmp_path):
    # A capitalized ECONOMY_NETWORK must not spuriously fail the match check.
    result = _config_subprocess(
        tmp_path,
        {
            "ECONOMY_ENABLED": "1",
            "XRPL_NETWORK": "mainnet",
            "ECONOMY_NETWORK": "  MainNet  ",
        },
        "import lfg_core.config as c; print(c.ECONOMY_NETWORK)",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "mainnet"
