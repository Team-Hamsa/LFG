# Regression for #323: the pytest suite must never inherit the deployed .env.
# lfg_core/config.py's bare load_dotenv() walks UP from CWD, so any worktree or
# checkout under the deployment tree froze the LIVE config at import (the #312
# BULK_MINT_UI_ENABLED=1 incident). config now gates the load on
# LFG_SKIP_DOTENV; these subprocess probes prove the gate in both directions.
# (Subprocess is mandatory — config constants freeze at first import, so an
# in-process re-import would not re-read the planted .env.)
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

import subprocess  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Everything lfg_core/config.py _require()s at import time, plus the layer
# knobs — supplied explicitly so the child interpreter can import config with
# the planted .env as its only other input.
_MANDATORY = {
    "XUMM_API_KEY": "test",
    "XUMM_API_SECRET": "test",
    "SEED": "sEdTM1uX8pu2do5XvTnutH6HsouMaM2",
    "TOKEN_ISSUER_ADDRESS": "rrrrrrrrrrrrrrrrrrrrrhoLvTp",
    "TOKEN_CURRENCY_HEX": "4C46474F00000000000000000000000000000000",
    "BUNNY_CDN_ACCESS_KEY": "test",
    "BUNNY_CDN_STORAGE_ZONE": "test",
    "LAYER_SOURCE": "local",
    "BUNNY_PULL_ZONE": "nft.pullzone.example",
    "XRPL_NETWORK": "testnet",
    "ECONOMY_NETWORK": "testnet",
}


def _probe(tmp_path, extra_env):
    """Import config in a child interpreter with CWD at tmp_path (where a
    hostile .env is planted) and print the frozen BULK_MINT_UI_ENABLED."""
    # Drop the suite's own pins so the child sees only what the case sets.
    env = {
        k: v for k, v in os.environ.items() if k not in ("BULK_MINT_UI_ENABLED", "LFG_SKIP_DOTENV")
    }
    env.update(_MANDATORY)
    env.update(extra_env)
    env["PYTHONPATH"] = _REPO_ROOT
    return subprocess.run(
        [sys.executable, "-c", "from lfg_core import config; print(config.BULK_MINT_UI_ENABLED)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.mark.parametrize(
    ("skip_env", "expected"),
    [
        ({"LFG_SKIP_DOTENV": "1"}, "False"),  # gate on: .env ignored, shipped default
        ({"LFG_SKIP_DOTENV": "0"}, "True"),  # gate explicitly off: .env honored
        ({}, "True"),  # gate unset (runtime posture): .env honored
    ],
)
def test_dotenv_gate(tmp_path, skip_env, expected):
    (tmp_path / ".env").write_text("BULK_MINT_UI_ENABLED=1\n")
    out = _probe(tmp_path, skip_env)
    assert out.stdout.strip() == expected, out.stderr


def test_hostile_ambient_export_still_wins_over_default(tmp_path):
    # An explicit env export must still reach config (setdefault semantics —
    # the gate only stops the .env FILE, not real environment variables).
    (tmp_path / ".env").write_text("")
    out = _probe(tmp_path, {"LFG_SKIP_DOTENV": "1", "BULK_MINT_UI_ENABLED": "1"})
    assert out.stdout.strip() == "True", out.stderr
