# #323: prove the root conftest.py alone isolates the suite from the deployed
# .env. Deliberately NO env-guard preamble — if conftest's central pins didn't
# supply the _require(...)-mandatory vars and set LFG_SKIP_DOTENV before
# lfg_core.config's first import, this module would fail at collection.
import os

from lfg_core import config


def test_suite_skips_dotenv() -> None:
    assert os.getenv("LFG_SKIP_DOTENV") not in (None, "0", "false", "False")


def test_shipped_default_not_env_masked() -> None:
    # Reads the FROZEN constant on purpose — the pre-#312 probe. Passes only
    # because conftest pinned the default and the deployed .env was never
    # loaded, regardless of what the box's .env says.
    assert config.BULK_MINT_UI_ENABLED is False


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
