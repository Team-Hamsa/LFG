# Legacy flag-24 harvest upgrade (burn + remint-as-blank of the SAME edition)
# races the on-chain listener on both halves of the pair — mainnet drift
# 2026-08-22 (37 spurious growth rows, 36 body-less burn rows):
#
#  * BURN: harvest's stamped burn row and the listener's out-of-band shrinkage
#    row are written under one unique index (whichever lands first wins), so
#    the two MUST carry identical deltas — the harvest mint row only balances
#    against a body-free burn row (Body travels via the edition_bodies pop).
#  * REMINT: harvest's own burn row has already popped the edition from the
#    effective genesis, so the blank remint looks like a brand-new edition to
#    `_apply_possible_growth`. A blank mint of an edition the supply ledger
#    already knows is the harvest's rebirth — the flow owns that row.
import asyncio
import sqlite3

from lfg_core import config, economy_flow, nft_listener
from lfg_core import economy_store as es
from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    return c


def _rec(edition=528, body="Straight Dark"):
    attrs = [{"trait_type": "Body", "value": body}]
    attrs += [{"trait_type": s, "value": "None"} for s in te.NON_BODY_SLOTS]
    attrs[1] = {"trait_type": "Background", "value": "Pastel Green"}
    return OnchainNft(
        nft_id="OLD528",
        nft_number=edition,
        owner="rOWNER",
        is_burned=False,
        mutable=False,
        uri_hex="",
        body="male",
        attributes=attrs,
        image="",
        ledger_index=1,
    )


def test_legacy_burn_row_matches_listener_shrinkage_row():
    rec = _rec()
    assert economy_flow._legacy_deltas(rec, sign=-1) == te.burn_shrinkage_deltas(rec)


def test_legacy_mint_row_is_body_free():
    rec = _rec()
    deltas = economy_flow._legacy_deltas(rec, sign=+1)
    assert not any(k.startswith("Body|") for k in deltas)
    assert deltas["Background|Pastel Green"] == 1
    assert len(deltas) == len(te.NON_BODY_SLOTS)


def _mint(conn, genesis, edition, meta_attrs):
    async def fetch_token(nft_id):
        return {
            "nft_id": "NEW528",
            "owner": config.SWAP_ISSUER_ADDRESS,
            "taxon": config.SWAP_TAXON,
            "uri_hex": "CD",
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return {"name": f"LFG #{edition}", "attributes": meta_attrs}

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "NEW528"},
    }
    _run(
        nft_listener.apply_economy_tx(
            conn, tx, fetch_token_fn=fetch_token, fetch_meta_fn=fetch_meta, genesis=genesis
        )
    )


def _mint_rows(conn):
    return [r for r in es.read_supply_changes(conn) if r["kind"] == "mint"]


def _burn_row(conn, actor):
    es.record_supply_change(
        conn,
        "burn",
        528,
        "Straight Dark",
        "male",
        {"Background|Pastel Green": -1},
        actor,
        "legacy harvest upgrade s1" if actor == "harvest" else "out-of-band burn OLD528",
        nft_id="OLD528",
    )


POPPED = te.Genesis(trait_counts={}, edition_bodies={})  # #528 popped by its burn row


def test_blank_remint_of_ledger_known_edition_logs_no_growth():
    """Harvest's burn row already popped #528; the blank remint is its rebirth,
    not a new edition — the flow writes the matching mint row itself."""
    conn = _conn()
    _burn_row(conn, "harvest")
    _mint(conn, POPPED, 528, te.blank_attributes())
    assert _mint_rows(conn) == []


def test_blank_remint_after_listener_won_burn_logs_no_growth():
    """Same race, other order: the listener's own shrinkage row won the burn
    (harvest's stamped row was ignored) — still no growth row on the remint."""
    conn = _conn()
    _burn_row(conn, "listener")
    _mint(conn, POPPED, 528, te.blank_attributes())
    assert _mint_rows(conn) == []


def test_dressed_remint_of_popped_edition_still_logs_growth():
    """A DRESSED mint of a popped edition is not a harvest rebirth (no flow
    remints a known edition dressed without its own row) — keep growth."""
    conn = _conn()
    _burn_row(conn, "listener")
    attrs = [{"trait_type": "Body", "value": "Straight Dark"}]
    attrs += [{"trait_type": s, "value": "None"} for s in te.NON_BODY_SLOTS]
    _mint(conn, POPPED, 528, attrs)
    assert len(_mint_rows(conn)) == 1


def test_blank_mint_of_never_seen_edition_still_logs_growth():
    """A blank mint of an edition with NO ledger history is genuine growth."""
    conn = _conn()
    _mint(conn, POPPED, 9001, te.blank_attributes())
    assert len(_mint_rows(conn)) == 1


def test_legacy_burn_row_carries_body_compensation_like_listener():
    """Worn Body ≠ genesis-recorded Body (bodies are swappable): the listener's
    shrinkage row adds `body_compensation_deltas`; harvest's burn row must too,
    or whichever wins the unique-index race decides whether Body balances."""
    rec = _rec(body="Ape")  # worn Ape, recorded Straight Dark
    effective = te.Genesis(trait_counts={}, edition_bodies={528: ("Straight Dark", "male")})
    expected = dict(te.burn_shrinkage_deltas(rec))
    expected.update(te.body_compensation_deltas(rec, effective))
    assert economy_flow._legacy_burn_deltas(rec, effective) == expected
    assert expected["Body|Ape"] == -1 and expected["Body|Straight Dark"] == 1
