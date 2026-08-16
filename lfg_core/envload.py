# lfg_core/envload.py — the single gated .env loader (#323).
#
# python-dotenv's bare load_dotenv() walks UP the directory tree from CWD, so
# any checkout/worktree under the deployment tree inherits the LIVE .env and
# config constants freeze against it at import time. The pytest suite opts out
# by setting LFG_SKIP_DOTENV=1 in the root conftest.py (before anything imports
# lfg_core), so tests exercise shipped defaults, never the box's .env. Runtime
# entrypoints (main.py / pm2 processes) never set the var and load normally.
#
# Every module that used to call load_dotenv() directly must call
# load_dotenv_unless_skipped() instead — a single ungated call anywhere
# re-opens the #312 bug class.
import os

from dotenv import load_dotenv


def load_dotenv_unless_skipped() -> None:
    if os.getenv("LFG_SKIP_DOTENV", "0") in ("0", "false", "False"):
        load_dotenv()
