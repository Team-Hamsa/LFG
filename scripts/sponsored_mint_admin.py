#!/usr/bin/env python3
"""Sponsored free-mint campaign switch that does not depend on Discord.

The Discord `/admin` panel's Start / Stop / Status buttons call
`/api/admin/sponsored-mint/*`, which only the Discord surface may reach — so a
Discord gateway outage (2026-09-15, "Session Unavailability") locks the operator
out of the switch while the campaign machinery itself (service + Activity) is
perfectly healthy. This script is the outage path: it runs ON THE DEPLOY BOX
and calls the same `lfg_core.sponsored_mint` functions the handlers do, against
the same box-local DBs.

    .venv/bin/python scripts/sponsored_mint_admin.py status --network mainnet
    .venv/bin/python scripts/sponsored_mint_admin.py start  --network mainnet
    .venv/bin/python scripts/sponsored_mint_admin.py stop   --network mainnet

Who can run it: only someone with a shell on the deploy box. It reads the box's
`.env` (the config import refuses to start without the mandatory secrets), the
box's `lfg_nfts.db` / `history_<net>.db`, and — for `start` — the clio endpoint
used to re-certify the archive. It exposes nothing over the network; a clone
of the public repo without the deployment's env and DBs can do nothing with it.

`start` mirrors the handler exactly: activate the campaign, then run the
archive re-verification the handler kicks in the background (#340) — here
synchronously, so the exit code tells you whether admission can actually open.
Exit codes: 0 ok · 1 refused by the library (e.g. mainnet self-issuer topology)
· 2 `--network` does not match `XRPL_NETWORK` · 3 campaign started but the
archive re-verify failed or crashed (admission stays fail-closed; fix the
archive, then `start` again or wait for the listener's auto catch-up) · 4
campaign started with `--skip-reverify`, so archive usability is UNKNOWN —
never treat 4 as "admission is open" · 5 archive verified usable (admission
can open) but the `archive_reverify` audit row could not be written — record
the run by hand.

Every state change (`start` / `stop`; `status` is a read and writes nothing)
lands in `free_mint_audit` with actor `cli:<os-login>` — the
identity is the account owning the process (resolved from the real UID via
`pwd`, so `$USER`/`$LOGNAME` cannot forge it; not a free-text flag), so
a reviewer can tell a shell-driven switch from a Discord one and who ran it.
`--note` appends a free-text label after it (`cli:<os-login>:<note>`).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pwd
import sys
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import archive_reverify, config, db_path, history_store, sponsored_mint  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_NETWORK_MISMATCH = 2
EXIT_REVERIFY_FAILED = 3
EXIT_REVERIFY_SKIPPED = 4
EXIT_AUDIT_WRITE_FAILED = 5


def _clio_client() -> Any:
    """Seam for tests: one websocket client on the configured clio endpoint."""
    from xrpl.asyncio.clients import AsyncWebsocketClient

    return AsyncWebsocketClient(config.CLIO_WS_URL)


async def _reverify(history_db: str, network: str) -> archive_reverify.ReverifyResult:
    # Same cross-process single-flight as the service (#402): the listener's
    # auto catch-up and backfill_history's certify modes hold this flock.
    lock = archive_reverify.acquire_certification_lock(history_db)
    if lock is None:
        return archive_reverify.ReverifyResult(
            False,
            "certification_lock_busy: another catch-up/certification run is active",
            None,
            None,
        )
    try:
        conn = history_store.init_history_db(history_db)
        try:
            async with _clio_client() as client:
                request_fn = archive_reverify.make_request_fn(client)
                return await archive_reverify.reverify_archive(conn, request_fn, network=network)
        finally:
            conn.close()
    finally:
        lock.close()


async def run_reverify(
    campaign_db: str, history_db: str, *, network: str, actor: str
) -> tuple[str | None, str | None]:
    """Re-certify the archive and wait for the listener heartbeat.

    Returns `(verify_error, audit_error)`: the archive verdict (None = usable)
    and, separately, whether the `archive_reverify` audit row was persisted
    (None = written) — an audit failure must not be misreported as "admission
    stays closed". Never raises: a crash anywhere in the sweep (clio
    unreachable, DB open failure, …) is reported as `internal_error: <exc>`
    so the caller still gets exit 3 and an audit row, the same way the
    service's run_archive_reverify logs-and-records instead of propagating.
    """
    error: str | None = None
    try:
        result = await _reverify(history_db, network)
        if not result.ok:
            error = result.reason or "reverify_failed"
        elif not await archive_reverify.wait_for_archive_usable(history_db, network=network):
            error = (
                "heartbeat_timeout: the listener has not restamped the archive heartbeat "
                "within 90s — on a quiet network wait for the next validated transaction and "
                "run start again; if the listener has no archive identity, set "
                "SPONSORED_MINT_*_GENESIS_HASH and restart the listener (never during a live "
                "campaign)"
            )
    except Exception as exc:  # noqa: BLE001 — must reach the audit row + exit code
        error = f"internal_error: {type(exc).__name__}: {exc}"
    try:
        sponsored_mint.audit_archive_reverify(
            campaign_db, network=network, actor=actor, result=f"failed: {error}" if error else "ok"
        )
    except Exception as exc:  # noqa: BLE001
        return error, f"{type(exc).__name__}: {exc}"
    return error, None


def _os_login() -> str:
    """Account owning this process, from the real UID — not $USER/$LOGNAME."""
    return pwd.getpwuid(os.getuid()).pw_name


def audit_actor(note: str = "") -> str:
    """`cli:<os-login>[:<note>]` — bound to the process owner, not a flag."""
    login = _os_login()
    note = note.strip()
    return f"cli:{login}:{note}" if note else f"cli:{login}"


def _print_status(status: sponsored_mint.CampaignStatus) -> None:
    print(json.dumps(dataclasses.asdict(status), indent=2, sort_keys=True))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("action", choices=["start", "stop", "status"])
    ap.add_argument("--network", default=config.XRPL_NETWORK, choices=["testnet", "mainnet"])
    ap.add_argument(
        "--note",
        default="",
        help="free-text label appended to the audit actor (identity is always the OS login)",
    )
    ap.add_argument(
        "--skip-reverify",
        action="store_true",
        help="start only: do not re-certify the archive (admission stays closed until it is usable)",
    )
    args = ap.parse_args()

    # Fail closed on a split network: the DB paths follow --network, but the
    # campaign row and the archive must belong to the chain this box serves.
    if args.network != config.XRPL_NETWORK:
        print(
            f"refusing: --network {args.network} but XRPL_NETWORK is {config.XRPL_NETWORK}; "
            f"the campaign would be flipped on the wrong chain's DB. "
            f"Re-run on the box whose XRPL_NETWORK={args.network}."
        )
        return EXIT_NETWORK_MISMATCH

    campaign_db = db_path.app_db_path(args.network)
    history_db = history_store.history_db_path(args.network)
    actor = audit_actor(args.note)

    if args.action == "status":
        _print_status(sponsored_mint.campaign_status(campaign_db, history_db, network=args.network))
        return EXIT_OK

    try:
        if args.action == "stop":
            _print_status(
                sponsored_mint.stop_campaign(campaign_db, network=args.network, actor=actor)
            )
            return EXIT_OK
        _print_status(sponsored_mint.start_campaign(campaign_db, network=args.network, actor=actor))
    except ValueError as exc:
        print(f"refused: {exc}")
        return EXIT_REFUSED

    if args.skip_reverify:
        print(
            "reverify: skipped — archive usability UNKNOWN; campaign is ACTIVE but admission "
            "opens only once the archive is usable (check `status`)."
        )
        return EXIT_REVERIFY_SKIPPED
    error, audit_error = asyncio.run(
        run_reverify(campaign_db, history_db, network=args.network, actor=actor)
    )
    if audit_error:
        print(f"WARNING: archive_reverify audit row not written: {audit_error}")
    if error:
        print(f"reverify: failed: {error}")
        print("campaign is ACTIVE but admission stays fail-closed until the archive is usable.")
        return EXIT_REVERIFY_FAILED
    print("reverify: ok — archive certified and heartbeat live; admission can open.")
    if audit_error:
        print("record this run in free_mint_audit by hand (archive is usable regardless).")
        return EXIT_AUDIT_WRITE_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
