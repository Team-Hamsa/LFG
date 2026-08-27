# lfg_service/identity.py
# Generalized identity: maps (platform, platform_user_id) -> XRPL wallet.
# The wallet is the canonical account; account_id is a reserved hook for
# future linked multi-surface profiles (nullable, unused now).

import hashlib
import json
import logging
import sqlite3
from typing import cast

from lfg_core.user_db import DATABASE  # single source of truth for the db path


def ensure_identities_table() -> None:
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS identities (
                platform          TEXT NOT NULL,
                platform_user_id  TEXT NOT NULL,
                platform_username TEXT,
                wallet            TEXT NOT NULL,
                account_id        INTEGER,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (platform, platform_user_id)
            )
            """
        )
        # Self-migrating, forward-only: add the #90 columns if an older table
        # shape is on disk, then backfill display_handle from the value we
        # already captured (platform_username). SQLite ADD COLUMN is non-
        # destructive; safe to run on every boot (mirrors migrate_users_*).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(identities)")}
        if "display_handle" not in cols:
            conn.execute("ALTER TABLE identities ADD COLUMN display_handle TEXT")
            conn.execute(
                "UPDATE identities SET display_handle = platform_username "
                "WHERE display_handle IS NULL"
            )
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE identities ADD COLUMN updated_at TIMESTAMP")
        # #135: per-user XUMM push token so future sign requests can be
        # push-delivered to Xaman instead of forcing a QR scan every time.
        if "user_token" not in cols:
            conn.execute("ALTER TABLE identities ADD COLUMN user_token TEXT")
        # #207: tables created before the account_id hook was reserved get the
        # column added here so profile attachment works on any on-disk shape.
        if "account_id" not in cols:
            conn.execute("ALTER TABLE identities ADD COLUMN account_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_identities_wallet ON identities(wallet)")
        # #206: append-only identity->wallet link history. identities.wallet is
        # an upsert (only the CURRENT wallet survives); this table keeps every
        # wallet an identity has ever linked, which is what the bucket resolver
        # walks. Never UPDATE/DELETE rows here.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_links (
                platform          TEXT NOT NULL,
                platform_user_id  TEXT NOT NULL,
                wallet            TEXT NOT NULL,
                linked_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (platform, platform_user_id, wallet)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_links_wallet ON wallet_links(wallet)")
        # #207: first-class profiles above per-platform identities. A profile
        # owns multiple identities (identities.account_id -> profiles.id) and,
        # through them, multiple wallets. merged_into implements append-only
        # merges: a losing profile row is never deleted, it points at its
        # winner so historical account_id references stay resolvable.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT,
                avatar_url   TEXT,
                preferences  TEXT NOT NULL DEFAULT '{}',
                merged_into  INTEGER,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_identities_account ON identities(account_id)")
        # #445: append-only wallet <-> XUMM user-token co-observation. The
        # issued_user_token is scoped per XUMM app + Xaman USER (identical
        # across every r-address in that install), so wallets that ever shared
        # a token belong to one human. The token ROTATES (30-day inactivity
        # expiry), so correlation is by recorded co-observation — never
        # live-token equality — and it is a push credential, so only a sha256
        # hash is stored here (identities.user_token keeps the raw value for
        # push delivery). Rows are never updated (beyond last_seen) or deleted.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_token_links (
                token_hash TEXT NOT NULL,
                wallet     TEXT NOT NULL,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen  TIMESTAMP,
                PRIMARY KEY (token_hash, wallet)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wallet_token_links_wallet ON wallet_token_links(wallet)"
        )
        # Seed observations from tokens already on file, so existing
        # multi-wallet Xaman users bucket immediately on deploy without
        # re-signing. Idempotent (INSERT OR IGNORE; hashing happens here
        # because sqlite has no sha256).
        for wallet, token in conn.execute(
            "SELECT wallet, user_token FROM identities WHERE user_token IS NOT NULL"
        ).fetchall():
            if wallet and token:
                conn.execute(
                    "INSERT OR IGNORE INTO wallet_token_links (token_hash, wallet) VALUES (?, ?)",
                    (_token_hash(token), wallet),
                )
        # Seed history from identities rows that predate wallet_links so
        # existing users participate in buckets. Idempotent (INSERT OR IGNORE).
        conn.execute(
            "INSERT OR IGNORE INTO wallet_links (platform, platform_user_id, wallet) "
            "SELECT platform, platform_user_id, wallet FROM identities"
        )
        conn.commit()
    finally:
        conn.close()


def link(
    platform: str,
    platform_user_id: str,
    platform_username: str,
    wallet: str,
    *,
    display_handle: str | None = None,
) -> bool:
    # display_handle defaults to platform_username when not supplied, so legacy
    # positional callers (register / signin) keep their existing behaviour while
    # the column is always populated and updated_at is stamped on every upsert.
    handle = display_handle if display_handle is not None else platform_username
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            """
            INSERT INTO identities
                (platform, platform_user_id, platform_username, display_handle, wallet, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(platform, platform_user_id) DO UPDATE SET
                platform_username = excluded.platform_username,
                display_handle = excluded.display_handle,
                wallet = excluded.wallet,
                updated_at = CURRENT_TIMESTAMP
            """,
            (platform, platform_user_id, platform_username, handle, wallet),
        )
        # #206: record the link in the append-only history alongside the upsert
        # (same transaction), so re-linking a new wallet never erases the old
        # association the bucket resolver depends on.
        conn.execute(
            "INSERT OR IGNORE INTO wallet_links (platform, platform_user_id, wallet) "
            "VALUES (?, ?, ?)",
            (platform, platform_user_id, wallet),
        )
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"identity.link failed: {e}")
        return False
    finally:
        conn.close()


def touch_handle(platform: str, platform_user_id: str, handle: str) -> None:
    """Best-effort refresh of a known identity's display_handle. No-op if the
    row doesn't exist or the handle is unchanged; never raises (caller treats
    this as a fire-and-forget side effect on authenticated touches)."""
    if not handle:
        return
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            "UPDATE identities SET display_handle = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE platform = ? AND platform_user_id = ? "
            "AND (display_handle IS NULL OR display_handle != ?)",
            (handle, platform, platform_user_id, handle),
        )
        conn.commit()
    except Exception as e:
        logging.error(f"identity.touch_handle failed: {e}")
    finally:
        if conn is not None:
            conn.close()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def observe_token(wallet: str | None, token: str | None) -> None:
    """Record a wallet <-> user-token co-observation (#445). Call ONLY with a
    token captured off a SIGNED payload whose signer was verified to be
    `wallet` — the link evidence is that signature, never a client assertion.
    Best-effort like set_user_token: falsy inputs are a no-op and DB errors
    are swallowed (a correlation write must never fail a sign flow)."""
    if not wallet or not token:
        return
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            "INSERT INTO wallet_token_links (token_hash, wallet) VALUES (?, ?) "
            "ON CONFLICT(token_hash, wallet) DO UPDATE SET last_seen = CURRENT_TIMESTAMP",
            (_token_hash(token), wallet),
        )
        conn.commit()
    except Exception as e:
        logging.error(f"identity.observe_token failed: {e}")
    finally:
        if conn is not None:
            conn.close()


def set_user_token(
    platform: str,
    platform_user_id: str,
    token: str | None,
    *,
    signer_wallet: str | None = None,
) -> None:
    """Persist the XUMM push token for an identity (issue #135). Best-effort:
    a falsy token or a missing identity row is a no-op, and DB errors are
    swallowed — a push-token write must never fail a sign flow. The identity
    row is created by link() at registration, so this only ever UPDATEs.

    signer_wallet (#445): the verified signer of the payload the token came
    off. When given, a wallet<->token co-observation is also recorded (see
    observe_token) — pass it only where signer == session wallet was checked."""
    if not token:
        return
    observe_token(signer_wallet, token)
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            "UPDATE identities SET user_token = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE platform = ? AND platform_user_id = ?",
            (token, platform, platform_user_id),
        )
        conn.commit()
    except Exception as e:
        logging.error(f"identity.set_user_token failed: {e}")
    finally:
        if conn is not None:
            conn.close()


def user_token_for(platform: str, platform_user_id: str) -> str | None:
    """The stored XUMM push token for an identity, or None if none is on file
    (unregistered, pre-#135 row, or the user never granted push). Never raises;
    a lookup failure returns None so the caller falls back to QR delivery."""
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute(
            "SELECT user_token FROM identities WHERE platform = ? AND platform_user_id = ?",
            (platform, platform_user_id),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        logging.error(f"identity.user_token_for failed: {e}")
        return None
    finally:
        if conn is not None:
            conn.close()


def resolve(platform: str, platform_user_id: str) -> str | None:
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute(
            "SELECT wallet FROM identities WHERE platform = ? AND platform_user_id = ?",
            (platform, platform_user_id),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logging.error(f"identity.resolve failed: {e}")
        return None
    finally:
        conn.close()


def identities_for_wallet(wallet: str) -> list[dict[str, object]]:
    """All surface identities linked to a wallet-account, ordered by created_at.

    Returns [] when none. The wallet is matched verbatim — XRPL classic
    addresses are case-sensitive (the base58check checksum makes a case-folded
    address invalid), so callers must NEVER lower-case the wallet.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute(
            "SELECT platform, platform_user_id, display_handle, platform_username, "
            "created_at, updated_at FROM identities WHERE wallet = ? ORDER BY created_at",
            (wallet,),
        )
        return [
            {
                "platform": r[0],
                "platform_user_id": r[1],
                "display_handle": r[2],
                "platform_username": r[3],
                "created_at": r[4],
                "updated_at": r[5],
            }
            for r in cur.fetchall()
        ]
    except Exception as e:
        logging.error(f"identity.identities_for_wallet failed: {e}")
        return []
    finally:
        if conn is not None:
            conn.close()


def handle_for_wallet(wallet: str) -> str | None:
    """Best display handle for a wallet, or None if no identity is linked.

    Most-recently-updated identity wins (falls back to created_at for rows
    that predate the updated_at column)."""
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute(
            "SELECT display_handle FROM identities WHERE wallet = ? "
            "AND display_handle IS NOT NULL "
            "ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 1",
            (wallet,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logging.error(f"identity.handle_for_wallet failed: {e}")
        return None
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# #206: identity buckets — "same human" resolution over shared wallets.
#
# Identities are keyed (platform, platform_user_id); the same human on Discord,
# Telegram, and web is three separate identities. Two identities belong to one
# logical-user BUCKET when they are connected through the append-only
# wallet_links history: connected components of the bipartite identity—wallet
# graph, so linkage is transitive (A—w1—B, B—w2—C puts A, B, C in one bucket)
# and permanent (an old, since-replaced wallet still links).
#
# Buckets are computed on demand, not materialized: the identities/wallet_links
# tables are tiny (hundreds of rows), each lookup is a bounded BFS over an
# indexed table, and on-demand computation can never go stale after a new link.
# If the table ever grows large enough to matter, swap in a maintained
# union-find table behind the same bucket_for() API.
#
# LEAKY BY DESIGN: the graph only captures users who link the SAME wallet on
# multiple platforms. A human who uses a different wallet per platform (and
# never cross-links) is multiple buckets — per-bucket gating (free-mint B2)
# deters casual double-claiming, it is not sybil-proof.
# ---------------------------------------------------------------------------


class BucketLookupError(Exception):
    """A bucket lookup failed for infrastructure reasons (DB error).

    Deliberately distinct from the None "unknown identity" result: bucket
    resolution backs per-human gating, so an infrastructure failure must fail
    CLOSED — callers must deny (or propagate), never treat it as "no bucket".
    This is an intentional exception to this module's never-raises style.
    """


def bucket_for(platform: str, platform_user_id: str) -> dict[str, object] | None:
    """Resolve the logical-user bucket containing an identity.

    Returns None ONLY for an unknown identity (no wallet_links row); raises
    BucketLookupError on any database failure (fail-closed — see above).
    Otherwise a dict:
      - "bucket_id": deterministic stable id — the JSON encoding of the
        lexicographically smallest [platform, platform_user_id] member key
        (unambiguous even when a field contains ":" or other separators;
        stable as long as that member exists; adding links can only merge
        buckets, never split them).
      - "identities": sorted list of {"platform", "platform_user_id"} members.
      - "wallets": sorted list of every wallet ever linked by any member.

    Wallets are matched verbatim — XRPL classic addresses are case-sensitive;
    never case-fold before calling.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        return _bucket_on_conn(conn, platform, platform_user_id)
    except Exception as e:
        logging.error(f"identity.bucket_for failed: {e}")
        raise BucketLookupError(f"bucket lookup failed for {platform!r}: {e}") from e
    finally:
        if conn is not None:
            conn.close()


def _bucket_on_conn(
    conn: sqlite3.Connection, platform: str, platform_user_id: str
) -> dict[str, object] | None:
    """bucket_for's BFS on a caller-provided connection, so a caller holding a
    write lock (ensure_profile_for's BEGIN IMMEDIATE) resolves the bucket
    INSIDE its transaction — snapshot, decision, and write all under one lock,
    no TOCTOU window against a concurrent link(). Raises raw DB errors; the
    caller wraps them (bucket_for -> BucketLookupError, ensure_profile_for ->
    ProfileError)."""
    seed = conn.execute(
        "SELECT 1 FROM wallet_links WHERE platform = ? AND platform_user_id = ? LIMIT 1",
        (platform, platform_user_id),
    ).fetchone()
    if not seed:
        return None
    return _bucket_bfs(conn, seed_ids={(platform, platform_user_id)}, seed_wallets=set())


def _bucket_bfs(
    conn: sqlite3.Connection,
    seed_ids: set[tuple[str, str]],
    seed_wallets: set[str],
) -> dict[str, object]:
    """Connected-component BFS over BOTH edge types: identity—wallet
    (wallet_links) and wallet—wallet via shared token hashes
    (wallet_token_links, #445). Alternates identity->wallet and
    wallet->{identity, token-sibling-wallet} expansions to fixpoint."""
    identities: set[tuple[str, str]] = set(seed_ids)
    wallets: set[str] = set(seed_wallets)
    frontier_ids: list[tuple[str, str]] = sorted(seed_ids)
    frontier_wallets: list[str] = sorted(seed_wallets)
    while frontier_ids or frontier_wallets:
        # identities -> their wallets
        new_wallets: set[str] = set(frontier_wallets)
        for p, uid in frontier_ids:
            for (w,) in conn.execute(
                "SELECT wallet FROM wallet_links WHERE platform = ? AND platform_user_id = ?",
                (p, uid),
            ):
                if w not in wallets:
                    new_wallets.add(w)
        wallets |= new_wallets
        frontier_ids = []
        frontier_wallets = []
        for w in new_wallets:
            # wallets -> their identities
            for p, uid in conn.execute(
                "SELECT platform, platform_user_id FROM wallet_links WHERE wallet = ?",
                (w,),
            ):
                if (p, uid) not in identities:
                    identities.add((p, uid))
                    frontier_ids.append((p, uid))
            # wallets -> token-sibling wallets (co-observed user tokens)
            for (w2,) in conn.execute(
                "SELECT DISTINCT l2.wallet FROM wallet_token_links l1 "
                "JOIN wallet_token_links l2 ON l2.token_hash = l1.token_hash "
                "WHERE l1.wallet = ?",
                (w,),
            ):
                if w2 not in wallets:
                    wallets.add(w2)
                    frontier_wallets.append(w2)
    members = sorted(identities)
    # A wallets-only bucket (web wallets known solely through token
    # observations) has no identity member to name it; fall back to the
    # smallest wallet under a reserved "wallet" pseudo-platform.
    bucket_key = list(members[0]) if members else ["wallet", sorted(wallets)[0]]
    return {
        "bucket_id": json.dumps(bucket_key),
        "identities": [{"platform": p, "platform_user_id": uid} for p, uid in members],
        "wallets": sorted(wallets),
    }


def bucket_for_wallet(wallet: str) -> dict[str, object] | None:
    """bucket_for keyed by wallet (#445) — the entry point web-surface callers
    need (their only key IS the wallet). Returns None for a wallet with no
    wallet_links row AND no token observation; raises BucketLookupError on DB
    failure (fail-closed, same contract as bucket_for). Wallets are matched
    verbatim — never case-fold."""
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        known = conn.execute(
            "SELECT 1 FROM wallet_links WHERE wallet = ? "
            "UNION SELECT 1 FROM wallet_token_links WHERE wallet = ? LIMIT 1",
            (wallet, wallet),
        ).fetchone()
        if not known:
            return None
        return _bucket_bfs(conn, seed_ids=set(), seed_wallets={wallet})
    except Exception as e:
        logging.error(f"identity.bucket_for_wallet failed: {e}")
        raise BucketLookupError(f"bucket lookup failed for wallet: {e}") from e
    finally:
        if conn is not None:
            conn.close()


def same_bucket(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """True when two (platform, platform_user_id) identities resolve to the
    same logical-user bucket. Unknown identities are never the same bucket.
    Raises BucketLookupError on DB failure (fail-closed; never a silent
    False)."""
    ba = bucket_for(*a)
    return (
        ba is not None and (bb := bucket_for(*b)) is not None and ba["bucket_id"] == bb["bucket_id"]
    )


def bucket_overlaps(
    platform: str,
    platform_user_id: str,
    *,
    identities: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    wallets: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """Per-bucket gating capability (#206, free-mint design decision B2).

    Given the sets a gate has already consumed — identity keys and/or wallets
    that have claimed a one-per-human benefit — returns True when ANY member of
    the caller's bucket appears in either set, i.e. the claim should be denied
    under per-human (B2) semantics.

    An unknown identity (no bucket) only matches on nothing, so this is safe to
    call unconditionally in front of an existing per-identity (B1) check: it
    can only widen the deny set, never grant.

    FAIL-CLOSED: a database failure raises BucketLookupError instead of
    quietly degrading to the direct-identity fallback (which could return
    False while a linked identity in the bucket has already claimed). Gating
    callers must treat the exception as deny.

    NOTE: this is a capability, not live wiring — the shipped sponsored free
    mint gates per-wallet on the SourceTag archive and is unchanged; the parked
    per-identity free-mint (#209) can adopt this at its call sites when
    revived. Leaky by design: only same-wallet linkage is caught.
    """
    bucket = bucket_for(platform, platform_user_id)
    if bucket is None:
        return (platform, platform_user_id) in identities
    members: list[dict[str, str]] = bucket["identities"]  # type: ignore[assignment]
    bucket_wallets: list[str] = bucket["wallets"]  # type: ignore[assignment]
    member_keys = {(m["platform"], m["platform_user_id"]) for m in members}
    if member_keys & set(identities):
        return True
    return bool(set(bucket_wallets) & set(wallets))


# ---------------------------------------------------------------------------
# #207: user profiles — a first-class entity above per-platform identities.
#
# One profile owns multiple platform identities (identities.account_id ->
# profiles.id, the reserved hook) and, through them, multiple wallets. The
# #206 bucket graph is one INPUT to membership: ensure_profile_for attaches a
# new identity to a bucket-mate's existing profile instead of creating a
# duplicate. FOUNDATION ONLY — nothing live reads profiles yet; per-human
# accounting (free mints, credits, leaderboards) migrates onto them later.
#
# Fail-closed, like buckets: profile resolution will back per-human
# accounting, so infrastructure errors raise (ProfileError /
# BucketLookupError) instead of silently creating or attaching the wrong
# profile. This is a deliberate exception to this module's never-raises style.
# ---------------------------------------------------------------------------


class ProfileError(Exception):
    """A profile operation failed (DB error, or an unknown identity).

    Fail-closed: callers must treat this as "no answer", never as "no
    profile" — silently proceeding could create or attach a wrong profile.
    """


def _resolve_merged(conn: sqlite3.Connection, profile_id: int) -> int:
    """Follow the merged_into chain to the surviving profile id."""
    seen: set[int] = set()
    while profile_id not in seen:
        seen.add(profile_id)
        row = conn.execute(
            "SELECT merged_into FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise ProfileError(f"profile {profile_id} does not exist")
        if row[0] is None:
            return profile_id
        profile_id = row[0]
    raise ProfileError(f"merged_into cycle at profile {profile_id}")


def _profile_row(conn: sqlite3.Connection, profile_id: int) -> dict[str, object]:
    row = conn.execute(
        "SELECT id, display_name, avatar_url, preferences, created_at FROM profiles WHERE id = ?",
        (profile_id,),
    ).fetchone()
    if row is None:
        raise ProfileError(f"profile {profile_id} does not exist")
    return {
        "id": row[0],
        "display_name": row[1],
        "avatar_url": row[2],
        "preferences": json.loads(row[3] or "{}"),
        "created_at": row[4],
    }


def _bucket_profile_ids(conn: sqlite3.Connection, bucket: dict[str, object]) -> list[int]:
    """Distinct surviving profile ids attached to any bucket member, ascending."""
    ids: set[int] = set()
    for m in cast(list[dict[str, str]], bucket["identities"]):
        row = conn.execute(
            "SELECT account_id FROM identities WHERE platform = ? AND platform_user_id = ?",
            (m["platform"], m["platform_user_id"]),
        ).fetchone()
        if row and row[0] is not None:
            ids.add(_resolve_merged(conn, row[0]))
    return sorted(ids)


def ensure_profile_for(platform: str, platform_user_id: str) -> dict[str, object]:
    """Find-or-create the profile for an identity.

    This is the CONVERGENCE POINT for a bucket: every call unifies ALL
    profiles attached to any member of the identity's #206 bucket (including
    the caller's own — an already-profiled identity whose bucket gained a
    second profile via a new wallet link is reconciled here, not skipped),
    then attaches the identity to the survivor. Only when the whole bucket is
    profile-less is a new profile created, seeded with the identity's
    display_handle. Deterministic winner: the oldest (smallest-id) profile —
    see merge_profiles.

    Serialized find-or-create: the ENTIRE read-decide-write — including the
    #206 bucket resolution itself (`_bucket_on_conn` on the locked connection)
    — runs inside a BEGIN IMMEDIATE transaction (the lfg_core/headroom.py
    precedent — one writer wins). Two overlapping ensures for the same
    identity or bucket cannot both observe "no profile" and double-insert,
    and a wallet link committed while this call waited for the lock IS seen
    (the bucket snapshot is taken after lock acquisition, so there is no
    stale-snapshot TOCTOU window against a concurrent link()).

    Raises ProfileError on infrastructure failure and for an unknown
    identity (fail-closed — never guesses).
    Returns the profile dict (profile_for()["profile"] shape) plus a
    "merge_reports" key: the conflict reports of any merges this call
    performed (see merge_profiles; [] when none). Conflicted merges are
    additionally logged at WARNING — conflict info is never silently
    discarded.
    """
    conn = None
    try:
        # isolation_level=None: explicit BEGIN IMMEDIATE / COMMIT control;
        # closing without COMMIT rolls back.
        conn = sqlite3.connect(DATABASE, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        # Bucket snapshot INSIDE the write lock — a concurrent link() either
        # committed before we acquired it (and is seen here) or is blocked
        # until we commit. Never resolve the bucket before BEGIN IMMEDIATE.
        bucket = _bucket_on_conn(conn, platform, platform_user_id)
        ident = conn.execute(
            "SELECT account_id, display_handle FROM identities "
            "WHERE platform = ? AND platform_user_id = ?",
            (platform, platform_user_id),
        ).fetchone()
        if ident is None:
            raise ProfileError(f"unknown identity ({platform!r}, {platform_user_id!r})")
        profile_ids = set(_bucket_profile_ids(conn, bucket)) if bucket else set()
        if ident[0] is not None:
            profile_ids.add(_resolve_merged(conn, ident[0]))
        merge_reports: list[dict[str, object]] = []
        if profile_ids:
            ordered = sorted(profile_ids)
            winner = ordered[0]
            for loser in ordered[1:]:
                report = _merge_in_conn(conn, winner, loser)
                if report["conflicts"]:
                    logging.warning(
                        "identity.ensure_profile_for merged profile %s into %s with conflicts: %s",
                        loser,
                        winner,
                        report["conflicts"],
                    )
                merge_reports.append(report)
        else:
            cur = conn.execute("INSERT INTO profiles (display_name) VALUES (?)", (ident[1],))
            winner = int(cur.lastrowid)  # type: ignore[arg-type]
        conn.execute(
            "UPDATE identities SET account_id = ? WHERE platform = ? AND platform_user_id = ?",
            (winner, platform, platform_user_id),
        )
        conn.execute("COMMIT")
        profile = _profile_row(conn, winner)
        profile["merge_reports"] = merge_reports
        return profile
    except ProfileError:
        raise
    except Exception as e:
        logging.error(f"identity.ensure_profile_for failed: {e}")
        raise ProfileError(f"ensure_profile_for failed: {e}") from e
    finally:
        if conn is not None:
            conn.close()


def _merge_in_conn(conn: sqlite3.Connection, winner: int, loser: int) -> dict[str, object]:
    """Merge loser into winner inside an open transaction. Winner's fields are
    kept verbatim; differing loser fields are reported, never applied."""
    w = _profile_row(conn, winner)
    lrow = _profile_row(conn, loser)
    conflicts: dict[str, object] = {}
    for field in ("display_name", "avatar_url"):
        if lrow[field] is not None and lrow[field] != w[field]:
            conflicts[field] = {"kept": w[field], "discarded": lrow[field]}
    w_prefs = cast(dict[str, object], w["preferences"])
    l_prefs = cast(dict[str, object], lrow["preferences"])
    pref_conflicts: dict[str, object] = {}
    for k, v in l_prefs.items():
        if k not in w_prefs or w_prefs[k] != v:
            pref_conflicts[k] = {"kept": w_prefs.get(k), "discarded": v}
    if pref_conflicts:
        conflicts["preferences"] = pref_conflicts
    moved = conn.execute(
        "UPDATE identities SET account_id = ? WHERE account_id = ?", (winner, loser)
    ).rowcount
    conn.execute("UPDATE profiles SET merged_into = ? WHERE id = ?", (winner, loser))
    return {
        "winner": winner,
        "loser": loser,
        "moved_identities": moved,
        "conflicts": conflicts,
    }


def merge_profiles(profile_id_a: int, profile_id_b: int) -> dict[str, object]:
    """Merge two profiles (e.g. a new wallet link joined two bucketed
    profiles). Deterministic winner: the OLDER profile (smaller id — ids are
    monotonic). Loser's identities move to the winner; the loser row survives
    with merged_into set, so re-merging is idempotent. Winner's
    display_name/avatar_url/preferences are kept; differing loser fields are
    surfaced in the returned conflict report, never silently merged.

    Raises ProfileError on DB failure or unknown profile ids (fail-closed).
    Returns {"winner", "loser", "moved_identities", "conflicts"}; merging a
    profile with itself (incl. already-merged pairs) is a no-op report.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        a = _resolve_merged(conn, profile_id_a)
        b = _resolve_merged(conn, profile_id_b)
        if a == b:
            return {"winner": a, "loser": None, "moved_identities": 0, "conflicts": {}}
        winner, loser = (a, b) if a < b else (b, a)
        report = _merge_in_conn(conn, winner, loser)
        conn.commit()
        return report
    except ProfileError:
        raise
    except Exception as e:
        logging.error(f"identity.merge_profiles failed: {e}")
        raise ProfileError(f"merge_profiles failed: {e}") from e
    finally:
        if conn is not None:
            conn.close()


def profile_for(platform: str, platform_user_id: str) -> dict[str, object] | None:
    """The profile an identity is attached to, or None if it has none yet
    (never creates — see ensure_profile_for). Raises ProfileError /
    BucketLookupError on infrastructure failure (fail-closed).

    Returns {"profile": {...}, "identities": [member identity keys],
    "wallets": [every wallet linked by any member, via the #206 bucket]}.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        row = conn.execute(
            "SELECT account_id FROM identities WHERE platform = ? AND platform_user_id = ?",
            (platform, platform_user_id),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        profile_id = _resolve_merged(conn, row[0])
        profile = _profile_row(conn, profile_id)
        members = [
            {"platform": r[0], "platform_user_id": r[1]}
            for r in conn.execute(
                "SELECT platform, platform_user_id FROM identities "
                "WHERE account_id = ? ORDER BY platform, platform_user_id",
                (profile_id,),
            )
        ]
    except ProfileError:
        raise
    except Exception as e:
        logging.error(f"identity.profile_for failed: {e}")
        raise ProfileError(f"profile_for failed: {e}") from e
    finally:
        if conn is not None:
            conn.close()
    bucket = bucket_for(platform, platform_user_id)  # may raise BucketLookupError
    wallets = list(cast(list[str], bucket["wallets"])) if bucket else []
    return {"profile": profile, "identities": members, "wallets": wallets}


def migrate_users_to_identities() -> int:
    """Copy legacy Users rows into identities as platform='discord'. Idempotent."""
    conn = sqlite3.connect(DATABASE)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if "Users" not in names:
            return 0
        rows = conn.execute("SELECT discord_id, discord_name, wallet FROM Users").fetchall()
        migrated = 0
        for discord_id, discord_name, wallet in rows:
            # #206: record the legacy wallet in the append-only history even
            # when an identities row already exists — the pre-upgrade Users
            # wallet may differ from the current identities.wallet, and losing
            # it would drop a linkage from the bucket graph. Idempotent.
            conn.execute(
                "INSERT OR IGNORE INTO wallet_links (platform, platform_user_id, wallet) "
                "VALUES ('discord', ?, ?)",
                (discord_id, wallet),
            )
            exists = conn.execute(
                "SELECT 1 FROM identities WHERE platform='discord' AND platform_user_id=?",
                (discord_id,),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO identities (platform, platform_user_id, platform_username, wallet) "
                "VALUES ('discord', ?, ?, ?)",
                (discord_id, discord_name, wallet),
            )
            migrated += 1
        conn.commit()
        return migrated
    finally:
        conn.close()
