import os
from pathlib import Path

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault(
    "TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000"
)
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")

from lfg_core import config


def test_batch_actions_are_dark_by_default():
    assert config.XRPL_ACTIONS_BATCH_ENABLED is False


def test_batch_action_limits_are_positive():
    assert config.XRPL_ACTIONS_LAST_LEDGER_OFFSET > 0
    assert config.XRPL_ACTIONS_TICKET_TARGET > 0
    assert config.XRPL_ACTIONS_CREATE_LIMIT > 0


def test_xrpl_py_floor_is_v5():
    requirements = Path("requirements.txt").read_text()
    assert "xrpl-py>=5.0.0" in requirements


def test_async_test_plugins_are_declared():
    requirements = Path("requirements.txt").read_text()
    assert "pytest-asyncio>=0.23" in requirements
    assert "pytest-aiohttp>=1.0" in requirements
