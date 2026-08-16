# #323: prove the root conftest.py alone isolates the suite from the deployed
# .env. Deliberately NO env-guard preamble — if conftest's central pins didn't
# supply the _require(...)-mandatory vars and set LFG_SKIP_DOTENV before
# lfg_core.config's first import, this module would fail at collection.
import os

from lfg_core import config


def test_suite_skips_dotenv() -> None:
    assert os.getenv("LFG_SKIP_DOTENV") not in (None, "0", "false", "False")


def test_frozen_constant_not_env_file_masked() -> None:
    # The pre-#312 probe, made invariant under the shell-export escape hatch:
    # assert CONSISTENCY, not a hardcoded value. The frozen constant must equal
    # a fresh env_flag() evaluation in this process — true whether the suite
    # runs bare (conftest fallback pin -> shipped default) or with an explicit
    # export (which legitimately wins). Only a .env FILE reaching config at
    # import (the bug this PR kills) could make the two diverge, because the
    # file loads after conftest's pins yet before config freezes.
    assert config.BULK_MINT_UI_ENABLED is config.env_flag(
        "BULK_MINT_UI_ENABLED", config.BULK_MINT_UI_ENABLED_DEFAULT
    )


def test_shipped_default_via_default_constant() -> None:
    # The convention's way to lock the shipped default, export-proof.
    assert config.BULK_MINT_UI_ENABLED_DEFAULT == "0"


def test_mandatory_vars_supplied_centrally() -> None:
    # _require() would have raised at import if any of these were missing.
    for name in (
        "XUMM_API_KEY",
        "XUMM_API_SECRET",
        "SEED",
        "TOKEN_ISSUER_ADDRESS",
        "TOKEN_CURRENCY_HEX",
        "BUNNY_CDN_ACCESS_KEY",
        "BUNNY_CDN_STORAGE_ZONE",
    ):
        assert os.getenv(name), name
