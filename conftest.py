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
#
# Only stdlib/pytest imports may sit above the pins: nothing here may import
# lfg_core before the environment below is in place.
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

# One throwaway root per session for every store the suite may reach; removed
# again in pytest_unconfigure (the old per-dir mkdtemps were never cleaned up).
#
# A pytest-xdist worker inherits the CONTROLLER's environment, so the pins the
# controller's import of this file already wrote would survive each worker's
# own setdefault below and every worker would share one sqlite file per store
# (verified: 4 workers, one lfg_nfts.db). Stamp the root we created into the
# environment so a child can tell "my parent's conftest generated this" from
# "the invoker deliberately exported it", and re-root only the former.
_INHERITED_STORE_ROOT = os.environ.get("LFG_TEST_STORE_ROOT")
_STORE_ROOT = tempfile.mkdtemp(prefix="lfg-test-stores-")
os.environ["LFG_TEST_STORE_ROOT"] = _STORE_ROOT


def _generated_by_parent(value: str | None) -> bool:
    """True for a pin some parent process's copy of this file produced."""
    if not value or not _INHERITED_STORE_ROOT:
        return False
    return value.startswith(_INHERITED_STORE_ROOT + os.sep)


def _store_path(name: str, *, mkdir: bool = False) -> str:
    path = os.path.join(_STORE_ROOT, name)
    if mkdir:
        os.makedirs(path, exist_ok=True)
    return path


# --- Durable job records must NEVER land in the checkout (2026-09-08) ---
# bulk_mint_flow / burn2mint_flow read their record directories from the
# environment at import. A test that escaped its per-file hermetic fixture
# (a bare monkeypatch.undo() also undid the autouse patch) wrote fixture
# burn-to-mint sessions — "validated" burns, wallet rUSERUSER…, network
# testnet — into ~/LFG/burn2mint_jobs/. The PROD service resumed them at
# its next boot and minted 32 real mainnet editions to an undeliverable
# wallet (the records are quarantined; see the incident write-up on the
# PR). Pin both dirs to a throwaway temp dir here, before any import, so no
# test can ever write a record the live service would trust. HARD-SET, not
# setdefault (CodeRabbit on #459): an inherited shell/pm2 export of these
# vars would otherwise point the suite straight at the live record dirs —
# the one case where "explicit export wins" must lose.
os.environ["BULK_MINT_JOBS_DIR"] = _store_path("bulk_mint_jobs", mkdir=True)
os.environ["BURN2MINT_JOBS_DIR"] = _store_path("burn2mint_jobs", mkdir=True)

# --- Per-network stores must NEVER resolve into the checkout (2026-09-16) ---
# Every store below defaults to a path relative to the CWD (app DB, X state,
# journal dirs, layer cache) or to the repo root (onchain/history DBs, the
# image archive). A test that reached one without its own monkeypatch wrote
# into whatever checkout pytest ran from — a worktree's gitignored
# lfg_nfts_testnet.db accumulated identities, sign_requests, free-mint
# claims, headroom reservations… and run from ~/LFG or ~/LFG-staging the same
# writes would land in the LIVE stores (image_archive.drop_archived even
# deletes from images_<net>/). Pin all of them into the session store root.
# setdefault, unlike the job dirs above: a deliberate export still wins, and
# the checkout-store guard at the bottom of this file fails the run if one
# points back into the checkout anyway. One file per store means both
# networks share it (DB_PATH / ONCHAIN_DB_PATH / HISTORY_DB_PATH override the
# per-network name); tests that need them apart already patch the path
# functions directly.
_STORE_PINS = {
    "DB_PATH": "lfg_nfts.db",
    "ONCHAIN_DB_PATH": "onchain.db",
    "HISTORY_DB_PATH": "history.db",
    "IMAGES_DIR": "images",
    "X_STATE_DB_PATH": "x_state.db",
    "ECONOMY_RECORDS_DIR": "economy_records",
    "SWAP_RECORDS_DIR": "swap_records",
    "LAYER_CACHE_DIR": "layer_cache",
    # Not a store, but the same leak: the CLI subprocess tests of
    # scripts/audit_layer_dimensions.py otherwise replace the checkout's
    # pre-push result cache with entries for their tmp fixtures.
    "LAYER_DIM_CACHE": "layer_dimensions_cache.json",
}
for _var, _name in _STORE_PINS.items():
    if _generated_by_parent(os.environ.get(_var)):
        del os.environ[_var]  # an xdist controller's pin, not a real override
    os.environ.setdefault(_var, _store_path(_name))
# Every pin a Python subprocess must carry. A test that builds a child env from
# scratch copies these in: `env.update({k: os.environ[k] for k in
# conftest.STORE_PIN_VARS})` (the guard below fails it otherwise).
STORE_PIN_VARS = ("BULK_MINT_JOBS_DIR", "BURN2MINT_JOBS_DIR", *_STORE_PINS)

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
# The Discord and Telegram surfaces have their own _require()-strict config
# modules on top of lfg_core.config. A test file that imports one inside a
# test body -- tests/test_discord_guild_sync.py, tests/test_telegram_buttons.py
# -- only ever passed because some OTHER file had already imported it with
# those vars patched in, leaving the module cached in sys.modules. That is a
# collection-order dependency: it breaks the moment the two files land on
# different pytest-xdist workers. Pin them centrally like every other
# mandatory var (setdefault, so a file's own monkeypatch still wins).
# The full import-time-mandatory set of both modules, so adding a surface
# import to a test file never needs a preamble:
#   discord  DISCORD_BOT_TOKEN LFG_SERVICE_URL SERVICE_TOKEN_DISCORD
#            ADMIN_LOG_CHANNEL_ID (+ SEED / XUMM_* / TOKEN_*, pinned above)
#   telegram TELEGRAM_BOT_TOKEN LFG_SERVICE_URL SERVICE_TOKEN_TELEGRAM
#            TELEGRAM_ANNOUNCE_CHAT_ID
# The two id vars must parse as a non-zero int; "1" is what every test file
# that patches them already uses.
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-discord-bot-token")
os.environ.setdefault("LFG_SERVICE_URL", "http://localhost:8000")
os.environ.setdefault("SERVICE_TOKEN_DISCORD", "test-discord-surface-token")
os.environ.setdefault("ADMIN_LOG_CHANNEL_ID", "1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-bot-token")
os.environ.setdefault("SERVICE_TOKEN_TELEGRAM", "test-telegram-surface-token")
os.environ.setdefault("TELEGRAM_ANNOUNCE_CHAT_ID", "1")

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
# monkeypatch.setenv (the helper reads the flag at call time). Assigned
# unconditionally — unlike the other pins there is no legitimate shell override:
# an inherited "1" would make the stubbed suites hit the network.
os.environ["PRESUBMIT_SIMULATE"] = "0"
# No busy-retry rounds suite-wide (real sleeps between failover passes);
# tests/test_xrpl_rpc_failover.py drives them explicitly. Assigned
# unconditionally: an inherited prod value would make unrelated tests sleep.
os.environ["XRPL_RPC_BUSY_RETRY_BACKOFF"] = ""
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


@pytest.fixture(autouse=True)
def _main_thread_event_loop() -> Iterator[None]:
    """Keep the legacy ``asyncio.get_event_loop()`` idiom working, always.

    The suite has no pytest-asyncio: sync tests drive coroutines through a
    local ``_run()`` helper, and several of those helpers still call
    ``asyncio.get_event_loop().run_until_complete(...)``. Any test that runs
    ``asyncio.run()`` (directly, or through a script's ``main()``) ends by
    calling ``set_event_loop(None)``, leaving the main thread with no current
    loop -- every later ``get_event_loop()`` then raises "There is no current
    event loop in thread 'MainThread'". Serial collection order happened to
    sort the poisoners after their victims; splitting the suite across
    pytest-xdist workers does not, and three files already carry a bespoke
    local workaround (test_headroom, test_house_closet_script,
    test_identity_token_links). Restore a usable loop before every test
    instead, so the idiom never depends on what ran first.
    """
    policy = asyncio.get_event_loop_policy()
    try:
        if not policy.get_event_loop().is_closed():
            yield
            return
    except RuntimeError:
        pass

    loop = policy.new_event_loop()
    policy.set_event_loop(loop)
    try:
        yield
    finally:
        # Close the loop we created whatever the test did with the current-loop
        # slot: asyncio.run() clears that slot without touching our loop, so
        # keying the close on "is it still current" would leak a selector and
        # its fds for exactly the tests that poisoned the slot in the first
        # place. The slot is a separate question -- clear it only while it
        # still points at our loop, so we never strand a closed loop there and
        # never disturb one the test installed itself.
        try:
            still_ours = policy.get_event_loop() is loop
        except RuntimeError:
            still_ours = False
        if still_ours:
            policy.set_event_loop(None)
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
        # RegularKey session rechecks (agent users §2): per-wallet answers and
        # failed-lookup back-offs are module state too.
        app_mod._regular_keys.clear()
        app_mod._regular_key_failed_at.clear()
        app_mod._regular_key_locks.clear()


@pytest.fixture(autouse=True)
def _reset_xrpl_rpc_state() -> Iterator[None]:
    # lfg_core.xrpl_rpc keeps process-level JSON-RPC state: per-URL failover
    # cooldowns and the verify_endpoint_chain endpoint restriction. Scrub both
    # around every test so one test's busy endpoint or restriction can't
    # reorder / narrow another's clients. (xrpl_rpc imports no lfg_core config,
    # so this lazy import freezes nothing.)
    from lfg_core import xrpl_rpc

    xrpl_rpc.reset_cooldowns()
    xrpl_rpc.clear_restriction()
    yield
    xrpl_rpc.reset_cooldowns()
    xrpl_rpc.clear_restriction()


@pytest.fixture(autouse=True)
def _isolated_payment_ledger(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # wait_for_payment now records consumed payments (issue #196); point the
    # ledger at a per-test file so tests never write the real app DB and a
    # tx hash consumed by one test can't fail the next.
    from lfg_core import payment_ledger

    monkeypatch.setattr(payment_ledger, "_db_path", lambda: str(tmp_path / "payment_ledger.db"))


# --- Checkout-store guard: fail the run if a test still reaches a live store ---
# The pins above only work if nothing bypasses them (a monkeypatch.delenv, a
# reloaded constant, a new store with its own default). This guard makes a
# bypass a hard failure instead of a silently growing file in the checkout:
#
# * An audit hook (sys.addaudithook) sees every in-process open / sqlite3
#   connect / mkdir / rename / remove / listdir and records any that names a
#   store inside a checkout root (the repo root, and the CWD pytest started
#   in). Attributing hits to PYTEST_CURRENT_TEST fails the offending test at
#   teardown; hits outside a test (collection, module import) fail the
#   session. Auditing THIS process — rather than diffing mtimes — keeps the
#   guard quiet in ~/LFG and ~/LFG-staging, where the live services write the
#   very same files while the suite runs.
# * A subprocess's own file access is invisible to the hook, so the hook also
#   watches subprocess.Popen: a Python child started without every store pin
#   (a from-scratch env=, or after a monkeypatch.delenv), or with one pointing
#   into the checkout, is a hit — a child can't write a checkout store it was
#   never pointed at. As a last net for other children, the session diffs the
#   checkout's store entries at start and finish and fails on any that
#   appeared or vanished (existence only, for the same live-writer reason).
_STORE_FILE_RE = re.compile(r"[^/]+\.db(?:-wal|-shm|-journal)?")
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# Non-*.db names written relative to the CWD: record/journal dirs, the layer
# cache, script reports (scripts/*.py REPORTS_DIR) and the pre-push layer
# dimension cache.
_STORE_NAMES = frozenset(
    {
        "bulk_mint_jobs",
        "burn2mint_jobs",
        "economy_records",
        "swap_records",
        ".layer_cache",
        "reports",
        ".layer_dimensions_cache.json",
        # mint_flow._save_recovery_record's admin-recovery dir (#466):
        # written relative to the CWD like the dirs above it, and became
        # reachable from ordinary test runs once ensure_offer's exhausted-
        # retries path started writing "minted_no_offer" records.
        "failed_db_records",
    }
)
_STORE_DIR_PREFIX = "images_"  # image_archive.archive_dir: images_<network>
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

# Audit event -> (path arg index, dir_fd arg index or None) per path it names.
# A dir_fd-relative path (shutil.rmtree walks that way) can't be resolved from
# the event, and it is never a checkout path in practice, so it is skipped.
_AUDITED_EVENTS: dict[str, tuple[tuple[int, int | None], ...]] = {
    "open": ((0, None),),
    "sqlite3.connect": ((0, None),),
    "os.listdir": ((0, None),),
    "os.scandir": ((0, None),),
    "os.mkdir": ((0, 2),),
    "os.remove": ((0, 1),),
    "os.rmdir": ((0, 1),),
    "os.rename": ((0, 2), (1, 3)),
    "shutil.rmtree": ((0, 1),),
}


def _is_store_name(name: str) -> bool:
    return (
        _STORE_FILE_RE.fullmatch(name) is not None
        or name in _STORE_NAMES
        or name.startswith(_STORE_DIR_PREFIX)
    )


def _may_name_store(path: str) -> bool:
    """Cheap pre-filter: can any component of `path` be a store name?"""
    if ".db" not in path and _STORE_DIR_PREFIX not in path:
        if not any(name in path for name in _STORE_NAMES):
            return False
    return any(_is_store_name(part) for part in path.split(os.sep))


def _sqlite_uri_path(uri: str) -> str:
    """Filesystem path of a SQLite `file:` URI: authority dropped, %-decoded."""
    path = uri[len("file:") :].split("?", 1)[0].split("#", 1)[0]
    if path.startswith("//"):  # file://host/abs or file:///abs
        path = "/" + path[2:].partition("/")[2]
    return urllib.parse.unquote(path)


def _is_python(executable: object) -> bool:
    try:
        exe = os.fsdecode(executable)  # type: ignore[arg-type]
    except TypeError:
        return False
    if os.path.basename(exe).startswith("python"):
        return True
    return os.path.realpath(exe) == os.path.realpath(sys.executable)


class _CheckoutStoreGuard:
    def __init__(self, roots: tuple[str, ...]) -> None:
        self.roots = tuple(
            dict.fromkeys(
                r for root in roots for r in (os.path.abspath(root), os.path.realpath(root))
            )
        )
        # (test id or "<outside a test>", audit event, checkout path)
        self.hits: list[tuple[str, str, str]] = []
        self.entries_at_start: set[str] = set()
        self.session_report: list[str] = []

    def inside_roots(self, path: str) -> str | None:
        """The checkout-relative part of `path` if it lies inside a root, else None."""
        for root in self.roots:
            prefix = root.rstrip(os.sep) + os.sep
            if path.startswith(prefix):
                return path[len(prefix) :]
        return None

    def store_path(self, path: str) -> str | None:
        """Where `path` lands if it names a store inside a checkout root, else None.

        Checks the literal absolute path and its realpath, so a symlink into the
        checkout counts whatever it is named (file or directory alias). The
        realpath stat per component costs ~1% of a full suite run.
        """
        abspath = os.path.abspath(path)
        for candidate in dict.fromkeys((abspath, os.path.realpath(abspath))):
            if not _may_name_store(candidate):
                continue
            rel = self.inside_roots(candidate)
            if rel is not None and _is_store_name(rel.split(os.sep, 1)[0]):
                return candidate
        return None

    def _record(self, event: str, what: str) -> None:
        test = os.environ.get("PYTEST_CURRENT_TEST", "<outside a test>")
        self.hits.append((test, event, what))

    def _audit_popen(self, args: tuple[object, ...]) -> None:
        executable, cwd, env = args[0], args[2], args[3]
        if not _is_python(executable):
            return
        child_env = os.environ if env is None else env
        if not isinstance(child_env, Mapping):
            return
        prog = os.path.basename(os.fsdecode(executable))  # type: ignore[arg-type]
        base = os.fsdecode(cwd) if cwd is not None else os.getcwd()  # type: ignore[arg-type]
        for var in STORE_PIN_VARS:
            value = child_env.get(var)
            if value is None and isinstance(child_env, dict):
                value = child_env.get(os.fsencode(var))  # os.environ rejects bytes keys
            if not value:  # empty reads as unset: the store falls back to its default
                self._record("subprocess.Popen", f"{prog} child env lacks {var}")
                continue
            target = os.path.realpath(os.path.join(base, os.fsdecode(value)))
            if self.inside_roots(target) is not None:
                self._record("subprocess.Popen", f"{prog} child env {var}={target}")

    def audit(self, event: str, args: tuple[object, ...]) -> None:
        if event == "subprocess.Popen":
            # (executable, args, cwd, env); env None = the child inherits ours.
            try:
                self._audit_popen(args)
            except Exception:
                pass  # an exception escaping an audit hook aborts the audited call
            return
        spec = _AUDITED_EVENTS.get(event)
        if spec is None:
            return
        # An exception escaping an audit hook aborts the audited call, so the
        # guard must never raise — at worst it misses an event.
        try:
            for path_index, dir_fd_index in spec:
                if path_index >= len(args):
                    continue
                if dir_fd_index is not None and dir_fd_index < len(args):
                    # os.* events report "no dir_fd" as -1, shutil.rmtree as None.
                    dir_fd = args[dir_fd_index]
                    if isinstance(dir_fd, int) and dir_fd >= 0:
                        continue
                raw = args[path_index]
                if isinstance(raw, int) or raw is None:
                    continue  # an fd, or os.listdir() of the CWD itself
                path = os.fsdecode(raw)  # type: ignore[arg-type]
                if event == "open" and args[1] is None and not os.path.isabs(path):
                    # os.open(): its event omits dir_fd, so a relative path may be
                    # fd-relative — shutil.rmtree's walk opens subdirs that way,
                    # read-only. Resolve against the CWD only an open that writes.
                    flags = args[2]
                    if not isinstance(flags, int) or not flags & _WRITE_FLAGS:
                        continue
                if event == "sqlite3.connect":
                    if path in ("", ":memory:"):
                        continue
                    if path.startswith("file:"):
                        path = _sqlite_uri_path(path)
                hit = self.store_path(path)
                if hit is not None:
                    self._record(event, hit)
        except Exception:
            return

    def store_entries(self) -> set[str]:
        entries: set[str] = set()
        for root in self.roots:
            try:
                names = os.listdir(root)
            except OSError:
                continue
            entries.update(
                os.path.join(root, name)
                for name in names
                if _is_store_name(name) and not name.endswith(_SQLITE_SIDECARS)
            )
        return entries


def _format_hits(hits: list[tuple[str, str, str]]) -> list[str]:
    return [f"{test}: {event} {path}" for test, event, path in dict.fromkeys(hits)]


_CHECKOUT_STORE_GUARD = _CheckoutStoreGuard(
    tuple(
        dict.fromkeys(
            (
                os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.realpath(__file__)),
                os.getcwd(),
            )
        )
    )
)
sys.addaudithook(_CHECKOUT_STORE_GUARD.audit)


@pytest.fixture(autouse=True)
def _no_checkout_store_access() -> Iterator[None]:
    start = len(_CHECKOUT_STORE_GUARD.hits)
    yield
    leaked = _CHECKOUT_STORE_GUARD.hits[start:]
    if leaked:
        pytest.fail(
            "test reached a store inside the checkout, or started a Python subprocess "
            "without conftest.STORE_PIN_VARS — pin or monkeypatch the path "
            "(see the store pins in the root conftest.py):\n  " + "\n  ".join(_format_hits(leaked)),
            pytrace=False,
        )


# --- Git config guard: fail the run if a test rewrites the enclosing repo's config ---
# Git exports GIT_DIR to its hooks and the pre-push gate runs this suite from
# one, so a temp-repo git command that inherits the environment acts on the
# OUTER repo. #373's first draft ran `git config user.name t` that way and left
# user.name=t / user.email=t@t in ~/LFG/.git/config — the one file every
# worktree shares — so each commit made from that clone was authored `t <t@t>`
# from 2026-08-16 until it was noticed on 2026-09-24. Tests scrub GIT_* and take
# their identity from GIT_AUTHOR_* / GIT_COMMITTER_*, never `git config`; this
# guard turns a regression into a failed run. The file is resolved with the
# inherited environment, so it is exactly the one a leaking `git config` would
# write. branch.* and remote.* are skipped: other git processes (worktree add
# with tracking, push -u, gh) rewrite those while a run is in flight.
_CONCURRENT_GIT_SECTIONS = ("branch.", "remote.")


def _enclosing_git_config(start: str) -> str | None:
    """The config file shared by every worktree of the repo at `start`, or None."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=start,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None  # not a checkout (or no git): nothing to guard
    return os.path.realpath(os.path.join(start, common, "config"))


class _GitConfigGuard:
    def __init__(self, path: str | None) -> None:
        self.path = path
        self.entries_at_start: Counter[str] = Counter()
        self.session_report: list[str] = []

    def entries(self) -> Counter[str]:
        """The config's `key=value` entries, minus the sections other git processes churn.

        A multiset: git reads the LAST value of a multi-valued key, so an
        `--add` repeating an earlier value is a change a set would not see.
        Raises OSError when git cannot read the file: a missing or malformed
        config lists nothing, which must not pass for an empty one.
        """
        if self.path is None:
            return Counter()
        listing = subprocess.run(
            ["git", "config", "--file", self.path, "--list", "--null"],
            capture_output=True,
            text=True,
        )
        if listing.returncode != 0:
            raise OSError(listing.stderr.strip() or f"git config exited {listing.returncode}")
        entries = (entry.replace("\n", "=", 1) for entry in listing.stdout.split("\0") if entry)
        return Counter(e for e in entries if not e.startswith(_CONCURRENT_GIT_SECTIONS))

    def changes(self) -> list[str]:
        try:
            entries = self.entries()
        except OSError as exc:
            return [f"unreadable after the run: {exc}"]
        return [f"added {e}" for e in sorted((entries - self.entries_at_start).elements())] + [
            f"removed {e}" for e in sorted((self.entries_at_start - entries).elements())
        ]


_GIT_CONFIG_GUARD = _GitConfigGuard(
    _enclosing_git_config(os.path.dirname(os.path.abspath(__file__)))
)


def pytest_sessionstart(session: pytest.Session) -> None:
    _CHECKOUT_STORE_GUARD.entries_at_start = _CHECKOUT_STORE_GUARD.store_entries()
    try:
        _GIT_CONFIG_GUARD.entries_at_start = _GIT_CONFIG_GUARD.entries()
    except OSError as exc:
        pytest.exit(f"git config guard cannot read {_GIT_CONFIG_GUARD.path}: {exc}", returncode=1)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    guard = _CHECKOUT_STORE_GUARD
    entries = guard.store_entries()
    report = _format_hits(guard.hits)
    report += [f"created during the run: {p}" for p in sorted(entries - guard.entries_at_start)]
    report += [f"removed during the run: {p}" for p in sorted(guard.entries_at_start - entries)]
    guard.session_report = report
    git_changes = _GIT_CONFIG_GUARD.changes()
    if git_changes:
        _GIT_CONFIG_GUARD.session_report = [
            f"{_GIT_CONFIG_GUARD.path} changed during the run — a test ran git with an "
            "inherited GIT_DIR, or outside its temp repo (scrub GIT_* and pass identity "
            "via GIT_AUTHOR_*/GIT_COMMITTER_*, never `git config`):",
            *(f"  {change}" for change in git_changes),
        ]
    if (report or git_changes) and session.exitstatus in (
        pytest.ExitCode.OK,
        pytest.ExitCode.NO_TESTS_COLLECTED,
    ):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    for title, report in (
        ("checkout store leak", _CHECKOUT_STORE_GUARD.session_report),
        ("git config leak", _GIT_CONFIG_GUARD.session_report),
    ):
        if not report:
            continue
        terminalreporter.section(title, sep="=", red=True, bold=True)
        for line in report:
            terminalreporter.line(line)


def pytest_unconfigure(config: pytest.Config) -> None:
    shutil.rmtree(_STORE_ROOT, ignore_errors=True)
