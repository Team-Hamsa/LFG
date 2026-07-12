# tests/test_backfill_market.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
# verbatim from tests/test_server_identity_wiring.py — same keys/values.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import backfill_market as bm  # noqa: E402

from lfg_core import market_store, xrpl_ops  # noqa: E402
from lfg_core.economy_store import _ECONOMY_SCHEMA, upsert_trait_token  # noqa: E402
from lfg_core.market_store import MarketListing  # noqa: E402
from lfg_core.nft_index import _SCHEMA as ONCHAIN_SCHEMA  # noqa: E402
from lfg_core.nft_index import OnchainNft  # noqa: E402
from lfg_core.nft_index import upsert as upsert_onchain_nft  # noqa: E402

SELLER = "rSellerAddress0000000000000000000"
BUYER = "rBuyerAddress000000000000000000000"
CHAR_NFT = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000019"
TRAIT_NFT = "000900001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000abc"


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Default ledger time for the whole file (#183): far in the future so no
# offer is treated as expired unless a test seeds a later one. Keeps the
# non-expiry tests hermetic — backfill now consults get_ledger_time once per
# run, and the real one would hit the network.
_STUB_LEDGER_TIME = 10_000_000_000


@pytest.fixture(autouse=True)
def _stub_ledger_time(monkeypatch):
    async def _fake() -> int:
        return _STUB_LEDGER_TIME

    monkeypatch.setattr(xrpl_ops, "get_ledger_time", _fake)


def _conn(tmp_path: Any) -> sqlite3.Connection:
    path = str(tmp_path / "onchain_test.db")
    c = sqlite3.connect(path)
    c.executescript(ONCHAIN_SCHEMA)
    c.executescript(_ECONOMY_SCHEMA)
    market_store.init_db(c)
    return c


def _seed_character(
    conn: sqlite3.Connection,
    nft_id: str = CHAR_NFT,
    owner: str = SELLER,
    is_burned: bool = False,
) -> None:
    upsert_onchain_nft(
        conn,
        OnchainNft(
            nft_id=nft_id,
            nft_number=42,
            owner=owner,
            is_burned=is_burned,
            mutable=True,
            uri_hex="",
            body="Ape",
            attributes=[{"trait_type": "Hat", "value": "Wizard Hat"}],
            image="https://cdn.example/img.png",
            ledger_index=1,
        ),
    )


def _seed_trait(
    conn: sqlite3.Connection,
    nft_id: str = TRAIT_NFT,
    owner: str = SELLER,
    slot: str = "Hat",
    value: str = "Wizard Hat",
) -> None:
    upsert_trait_token(conn, nft_id, owner, slot, value)


def _sell_offer(
    offer_index: str,
    owner: str = SELLER,
    amount: str = "1000000",
    destination: str | None = None,
    flags: int = bm.market_ops.LSF_SELL_NFTOKEN,
    expiration: int | None = None,
) -> dict[str, Any]:
    return {
        "offer_index": offer_index,
        "amount": amount,
        "destination": destination,
        "flags": flags,
        "owner": owner,
        "expiration": expiration,
    }


def _fetch_offers_map(mapping: dict[str, list[dict[str, Any]]]) -> Any:
    """Build a fetch_offers(nft_id) stand-in from a {nft_id: [offer, ...]} map;
    unmapped nft_ids return []  (mirrors get_nft_sell_offers' no-offers case)."""

    async def fetch(nft_id: str) -> list[dict[str, Any]]:
        return mapping.get(nft_id, [])

    return fetch


# --- sweep: correct kind/slot/value -----------------------------------------


def test_sweep_character_upserts_live_row(tmp_path):
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)
    fetch = _fetch_offers_map({CHAR_NFT: [_sell_offer("OFF_CHAR", owner=SELLER)]})

    stats = _run(bm.backfill_market(conn, fetch_offers=fetch))

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_CHAR'").fetchone()
    assert row is not None
    assert row["nft_id"] == CHAR_NFT
    assert row["kind"] == "character"
    assert row["seller"] == SELLER
    assert row["amount_drops"] == 1_000_000
    assert row["is_live"] == 1
    assert row["slot"] is None and row["value"] is None
    assert stats["live_listings"] == 1


def test_sweep_trait_copies_slot_value(tmp_path):
    conn = _conn(tmp_path)
    _seed_trait(conn, TRAIT_NFT, owner=SELLER, slot="Hat", value="Wizard Hat")
    fetch = _fetch_offers_map({TRAIT_NFT: [_sell_offer("OFF_TRAIT", owner=SELLER)]})

    _run(bm.backfill_market(conn, fetch_offers=fetch))

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_TRAIT'").fetchone()
    assert row is not None
    assert row["kind"] == "trait"
    assert row["slot"] == "Hat"
    assert row["value"] == "Wizard Hat"


def test_sweep_covers_both_populations_in_stats(tmp_path):
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)
    _seed_trait(conn, TRAIT_NFT, owner=SELLER)
    fetch = _fetch_offers_map({})

    stats = _run(bm.backfill_market(conn, fetch_offers=fetch))

    assert stats["characters_swept"] == 1
    assert stats["traits_swept"] == 1


# --- filtering: buy offers, IOU amounts, stale seller -----------------------


def test_buy_offer_not_upserted(tmp_path):
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)
    fetch = _fetch_offers_map({CHAR_NFT: [_sell_offer("OFF_BUY", owner=SELLER, flags=0)]})

    _run(bm.backfill_market(conn, fetch_offers=fetch))

    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0


def test_iou_amount_not_upserted(tmp_path):
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)
    offer = _sell_offer("OFF_IOU", owner=SELLER)
    offer["amount"] = {"currency": "USD", "issuer": "rIssuer", "value": "10"}
    fetch = _fetch_offers_map({CHAR_NFT: [offer]})

    _run(bm.backfill_market(conn, fetch_offers=fetch))

    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0


def test_stale_seller_offer_not_upserted(tmp_path):
    """A sell offer still on-ledger from a PREVIOUS owner (current owner has
    since moved on) must not be treated as a live listing."""
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=BUYER)  # current owner is the buyer now
    fetch = _fetch_offers_map({CHAR_NFT: [_sell_offer("OFF_STALE_SELLER", owner=SELLER)]})

    _run(bm.backfill_market(conn, fetch_offers=fetch))

    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0


def test_destination_locked_offer_is_still_stored(tmp_path):
    """Destination-locked offers are stored (browse hides them via
    destination IS NULL, per market_store) -- backfill must not filter them
    out itself, matching the listener's offer_create handling."""
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)
    fetch = _fetch_offers_map(
        {CHAR_NFT: [_sell_offer("OFF_DEST", owner=SELLER, destination=BUYER)]}
    )

    _run(bm.backfill_market(conn, fetch_offers=fetch))

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_DEST'").fetchone()
    assert row is not None
    assert row["destination"] == BUYER
    assert row["is_live"] == 1


# --- idempotent re-run -------------------------------------------------------


def test_rerun_idempotent_no_dupes(tmp_path):
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)
    fetch = _fetch_offers_map({CHAR_NFT: [_sell_offer("OFF_CHAR", owner=SELLER)]})

    _run(bm.backfill_market(conn, fetch_offers=fetch))
    _run(bm.backfill_market(conn, fetch_offers=fetch))

    rows = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_CHAR'").fetchall()
    assert len(rows) == 1


def _all_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in conn.execute("SELECT * FROM market_listings ORDER BY offer_index").fetchall()
    ]


def test_rerun_idempotent_field_for_field_including_listener_timestamps(tmp_path):
    """The sweep re-confirming a LISTENER-written row (real created_ledger/
    created_ts, which the backfill itself cannot know) must not change a
    single field of it — this is the test that catches a backfill wiping the
    timestamps, and full-row-set identity across runs is the real
    idempotency claim."""
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)
    # Listener-style row: real timestamps, offer still live on-ledger.
    market_store.upsert_listing(
        conn,
        MarketListing(
            offer_index="OFF_LISTENER",
            nft_id=CHAR_NFT,
            kind="character",
            seller=SELLER,
            amount_drops=1_000_000,
            created_ledger=555,
            created_ts=1_700_000_000,
            is_live=1,
        ),
    )
    fetch = _fetch_offers_map({CHAR_NFT: [_sell_offer("OFF_LISTENER", owner=SELLER)]})

    _run(bm.backfill_market(conn, fetch_offers=fetch))
    after_first = _all_rows(conn)
    _run(bm.backfill_market(conn, fetch_offers=fetch))
    after_second = _all_rows(conn)

    assert after_first == after_second
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_LISTENER'").fetchone()
    assert row["created_ledger"] == 555
    assert row["created_ts"] == 1_700_000_000
    assert row["is_live"] == 1


# --- transient fetch failure must not stale-close ----------------------------


def test_fetch_failure_excludes_token_from_stale_close(tmp_path):
    """A per-token RPC failure is NOT "no offers": the failed token's live
    listing must survive untouched, other tokens must still reconcile, and
    the summary must report the failure count."""
    conn = _conn(tmp_path)
    ok_nft = CHAR_NFT
    bad_nft = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000BAD"
    _seed_character(conn, ok_nft, owner=SELLER)
    _seed_character(conn, bad_nft, owner=SELLER)
    for offer_index, nft_id in (("OFF_OK_GONE", ok_nft), ("OFF_BAD_LIVE", bad_nft)):
        market_store.upsert_listing(
            conn,
            MarketListing(
                offer_index=offer_index,
                nft_id=nft_id,
                kind="character",
                seller=SELLER,
                amount_drops=1_000_000,
                is_live=1,
            ),
        )

    async def fetch(nft_id: str) -> list[dict[str, Any]]:
        if nft_id == bad_nft:
            raise RuntimeError("rpc blip")
        return []  # ok_nft: genuinely no offers on-ledger

    stats = _run(bm.backfill_market(conn, fetch_offers=fetch))

    conn.row_factory = sqlite3.Row
    bad_row = conn.execute(
        "SELECT * FROM market_listings WHERE offer_index='OFF_BAD_LIVE'"
    ).fetchone()
    ok_row = conn.execute(
        "SELECT * FROM market_listings WHERE offer_index='OFF_OK_GONE'"
    ).fetchone()
    assert bad_row["is_live"] == 1 and bad_row["closed_reason"] is None  # survived the blip
    assert ok_row["is_live"] == 0 and ok_row["closed_reason"] == "stale"  # still reconciled
    assert stats["fetch_failures"] == 1
    assert stats["closed_stale"] == 1


# --- stale close: previously-live row absent from ledger --------------------


def test_previously_live_row_absent_on_ledger_closed_stale(tmp_path):
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)
    market_store.upsert_listing(
        conn,
        MarketListing(
            offer_index="OFF_GONE",
            nft_id=CHAR_NFT,
            kind="character",
            seller=SELLER,
            amount_drops=1_000_000,
            is_live=1,
        ),
    )
    fetch = _fetch_offers_map({CHAR_NFT: []})  # offer no longer on-ledger

    stats = _run(bm.backfill_market(conn, fetch_offers=fetch))

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_GONE'").fetchone()
    assert row["is_live"] == 0
    assert row["closed_reason"] == "stale"
    assert stats["closed_stale"] == 1


def test_expired_offer_not_upserted_live(tmp_path):
    """#183: an offer whose Expiration is at/before the current ledger time is
    dead — the sweep must not record it as live."""
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)
    fetch = _fetch_offers_map(
        {CHAR_NFT: [_sell_offer("OFF_EXPIRED", owner=SELLER, expiration=_STUB_LEDGER_TIME - 1)]}
    )

    stats = _run(bm.backfill_market(conn, fetch_offers=fetch))

    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_EXPIRED'").fetchone()
    assert row is None
    assert stats["live_listings"] == 0


def test_expired_previously_live_row_closed_stale(tmp_path):
    """A row that was live but whose backing offer has since expired is retired
    by the stale-close pass (the expired offer drops out of this sweep)."""
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)
    market_store.upsert_listing(
        conn,
        MarketListing(
            offer_index="OFF_EXPIRED",
            nft_id=CHAR_NFT,
            kind="character",
            seller=SELLER,
            amount_drops=1_000_000,
            is_live=1,
        ),
    )
    fetch = _fetch_offers_map(
        {CHAR_NFT: [_sell_offer("OFF_EXPIRED", owner=SELLER, expiration=_STUB_LEDGER_TIME - 1)]}
    )

    stats = _run(bm.backfill_market(conn, fetch_offers=fetch))

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_EXPIRED'").fetchone()
    assert row["is_live"] == 0
    assert row["closed_reason"] == "stale"
    assert stats["closed_stale"] == 1


def test_ledger_time_failure_leaves_expiry_unjudged(tmp_path):
    """If the ledger-time fetch fails, expiry is left unjudged: an offer that
    LOOKS expired is still upserted live (fail-open on the filter) rather than
    risk falsely closing every expiring listing over a transient blip."""
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)

    async def boom() -> int:
        raise RuntimeError("ledger rpc down")

    fetch = _fetch_offers_map(
        {CHAR_NFT: [_sell_offer("OFF_MAYBE_EXPIRED", owner=SELLER, expiration=1)]}
    )

    _run(bm.backfill_market(conn, fetch_offers=fetch, fetch_ledger_time=boom))

    row = conn.execute(
        "SELECT is_live FROM market_listings WHERE offer_index='OFF_MAYBE_EXPIRED'"
    ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_still_live_offer_not_closed_stale(tmp_path):
    conn = _conn(tmp_path)
    _seed_character(conn, CHAR_NFT, owner=SELLER)
    market_store.upsert_listing(
        conn,
        MarketListing(
            offer_index="OFF_STILL_LIVE",
            nft_id=CHAR_NFT,
            kind="character",
            seller=SELLER,
            amount_drops=1_000_000,
            is_live=1,
        ),
    )
    fetch = _fetch_offers_map({CHAR_NFT: [_sell_offer("OFF_STILL_LIVE", owner=SELLER)]})

    _run(bm.backfill_market(conn, fetch_offers=fetch))

    row = conn.execute(
        "SELECT is_live, closed_reason FROM market_listings WHERE offer_index='OFF_STILL_LIVE'"
    ).fetchone()
    assert tuple(row) == (1, None)


# --- settled preserved across re-runs ---------------------------------------


def test_settled_trait_sale_not_resurrected_or_touched(tmp_path):
    """A sold, settled=0 trait row (burn-back-to-Closet still pending) must
    not be flipped back to live, and `settled` must not be touched, even
    though the trait token's on-chain owner has moved to the buyer and the
    accepted sell offer is gone from the ledger."""
    conn = _conn(tmp_path)
    _seed_trait(conn, TRAIT_NFT, owner=BUYER, slot="Hat", value="Wizard Hat")  # ownership moved
    market_store.upsert_listing(
        conn,
        MarketListing(
            offer_index="OFF_SOLD_TRAIT",
            nft_id=TRAIT_NFT,
            kind="trait",
            seller=SELLER,
            amount_drops=500_000,
            slot="Hat",
            value="Wizard Hat",
            is_live=1,
        ),
    )
    market_store.close_listing(conn, "OFF_SOLD_TRAIT", "sold")  # settled -> 0
    fetch = _fetch_offers_map({TRAIT_NFT: []})  # accepted offer is gone from the ledger

    _run(bm.backfill_market(conn, fetch_offers=fetch))

    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM market_listings WHERE offer_index='OFF_SOLD_TRAIT'"
    ).fetchone()
    assert row["is_live"] == 0
    assert row["closed_reason"] == "sold"
    assert row["settled"] == 0


def test_settled_marked_trait_sale_left_alone(tmp_path):
    """A trait sale that has already completed settlement (settled=1) is
    likewise never revisited by the sweep."""
    conn = _conn(tmp_path)
    _seed_trait(conn, TRAIT_NFT, owner=BUYER, slot="Hat", value="Wizard Hat")
    market_store.upsert_listing(
        conn,
        MarketListing(
            offer_index="OFF_SETTLED_TRAIT",
            nft_id=TRAIT_NFT,
            kind="trait",
            seller=SELLER,
            amount_drops=500_000,
            slot="Hat",
            value="Wizard Hat",
            is_live=1,
        ),
    )
    market_store.close_listing(conn, "OFF_SETTLED_TRAIT", "sold")
    market_store.mark_settled(conn, "OFF_SETTLED_TRAIT")
    fetch = _fetch_offers_map({TRAIT_NFT: []})

    _run(bm.backfill_market(conn, fetch_offers=fetch))

    row = conn.execute(
        "SELECT is_live, closed_reason, settled FROM market_listings WHERE offer_index='OFF_SETTLED_TRAIT'"
    ).fetchone()
    assert tuple(row) == (0, "sold", 1)


def test_network_arg_defaults_to_config_like_backfill_onchain(monkeypatch):
    """--network default parity with scripts/backfill_onchain.py (#130):
    both backfills should run against config.XRPL_NETWORK when --network is
    omitted, instead of one requiring the flag. Pin a known-valid network so
    the machine's env (which may carry an out-of-choices value) can't turn
    the flag required and SystemExit this test (CodeRabbit #150)."""
    from lfg_core import config

    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")
    args = bm._build_parser().parse_args([])
    assert args.network == "testnet"


def test_network_arg_explicit_choices_still_work():
    assert bm._build_parser().parse_args(["--network", "mainnet"]).network == "mainnet"
    assert bm._build_parser().parse_args(["--network", "testnet"]).network == "testnet"


def test_network_arg_bad_env_default_fails_fast(monkeypatch):
    """Greptile #150: argparse never validates a default against choices, so
    an unexpected XRPL_NETWORK (e.g. 'devnet') would silently flow into
    index_db_path and create/rebuild the wrong DB. With a bad env value the
    flag must become required (omitting it errors), and an explicit valid
    choice must still work."""
    import pytest

    from lfg_core import config

    monkeypatch.setattr(config, "XRPL_NETWORK", "devnet")
    with pytest.raises(SystemExit):
        bm._build_parser().parse_args([])
    assert bm._build_parser().parse_args(["--network", "testnet"]).network == "testnet"
