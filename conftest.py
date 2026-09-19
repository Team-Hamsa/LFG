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
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

# One throwaway root per session for every store the suite may reach; removed
# again in pytest_unconfigure (the old per-dir mkdtemps were never cleaned up).
_STORE_ROOT = tempfile.mkdtemp(prefix="lfg-test-stores-")


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
# tests/test_xrpl_rpc_failover.py drives them explicitly.
os.environ.setdefault("XRPL_RPC_BUSY_RETRY_BACKOFF", "")
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


def pytest_sessionstart(session: pytest.Session) -> None:
    _CHECKOUT_STORE_GUARD.entries_at_start = _CHECKOUT_STORE_GUARD.store_entries()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    guard = _CHECKOUT_STORE_GUARD
    entries = guard.store_entries()
    report = _format_hits(guard.hits)
    report += [f"created during the run: {p}" for p in sorted(entries - guard.entries_at_start)]
    report += [f"removed during the run: {p}" for p in sorted(guard.entries_at_start - entries)]
    guard.session_report = report
    if report and session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not _CHECKOUT_STORE_GUARD.session_report:
        return
    terminalreporter.section("checkout store leak", sep="=", red=True, bold=True)
    for line in _CHECKOUT_STORE_GUARD.session_report:
        terminalreporter.line(line)


def pytest_unconfigure(config: pytest.Config) -> None:
    shutil.rmtree(_STORE_ROOT, ignore_errors=True)
