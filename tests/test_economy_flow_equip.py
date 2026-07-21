# Equip flow: move a loose asset onto a live character; displaced -> Bucket.
# Driven through injected fakes — no network.

import asyncio
import json
import sqlite3

from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft
from tests.economy_helpers import flaky_mirror_conn

NON_BODY = te.NON_BODY_SLOTS
OLD_URL = "https://cdn/old.json"
OLD_URI_HEX = OLD_URL.encode().hex().upper()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _char() -> OnchainNft:
    attrs = [{"trait_type": "Body", "value": "Straight Blue"}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return OnchainNft(
        nft_id="NFT7",
        nft_number=7,
        owner="rUser",
        is_burned=False,
        mutable=True,
        uri_hex=OLD_URI_HEX,
        body="male",
        attributes=attrs,
        image="",
        ledger_index=1,
    )


def _conn_with_bucket() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    es.freeze_genesis(
        c, te.Genesis(trait_counts={}, edition_bodies={7: ("Straight Blue", "male")}), {}
    )
    es.set_closet_token(c, "rUser", "CLOSET", "00")
    es.set_closet_contents(c, "rUser", [("Head", "Crown", 1)], [])
    return c


class _Fakes:
    def __init__(
        self, *, fail_closet_modify=False, raise_closet_modify=False, fail_revert_modify=False
    ) -> None:
        self.fail_closet_modify = fail_closet_modify
        self.raise_closet_modify = raise_closet_modify
        # When set, the REVERT char_modify (the 2nd+ call) returns a falsy hash,
        # simulating a modify-back that did not land on-ledger.
        self.fail_revert_modify = fail_revert_modify
        self.char_modifies: list[tuple[str, str, str]] = []

    async def closet_upload(self, meta: dict) -> str:
        return "https://cdn/b.json"

    async def closet_mint(self, url):
        return "CLOSET"

    async def closet_offer(self, nft_id, owner):
        return "O"

    async def closet_accept(self, offer_id):
        return {}

    async def closet_modify(self, nft_id, owner, url):
        if self.raise_closet_modify:
            raise RuntimeError("timeout after submit")
        return None if self.fail_closet_modify else "MODHASH"

    async def char_compose(self, attrs, body, edition, rev):
        return ("img", None, "https://cdn/new.json")

    async def char_mint(self, url):
        return "CHAR"

    async def char_modify(self, nft_id, owner, url):
        is_revert = len(self.char_modifies) > 0
        self.char_modifies.append((nft_id, owner, url))
        if is_revert and self.fail_revert_modify:
            return None  # the modify-back did not land
        return "MODH"

    async def char_burn(self, nft_id, owner):
        return "BURN"

    async def char_offer(self, nft_id, owner):
        return "O"

    async def char_accept(self, offer_id):
        return {}


def _deps(conn, f, records_dir):
    return ef.EconomyDeps(
        conn=conn,
        closet_upload_fn=f.closet_upload,
        closet_mint_fn=f.closet_mint,
        closet_offer_fn=f.closet_offer,
        closet_accept_fn=f.closet_accept,
        closet_modify_fn=f.closet_modify,
        char_compose_fn=f.char_compose,
        char_mint_fn=f.char_mint,
        char_modify_fn=f.char_modify,
        char_burn_fn=f.char_burn,
        char_offer_fn=f.char_offer,
        char_accept_fn=f.char_accept,
        records_dir=str(records_dir),
    )


def test_equip_happy_path(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    assert s.displaced == {"Head": "None"}
    assert f.char_modifies == [("NFT7", "rUser", "https://cdn/new.json")]
    assets = {(slot, v): n for o, slot, v, n in es.read_closet_assets(conn)}
    assert ("Head", "Crown") not in assets  # incoming consumed
    assert assets[("Head", "None")] == 1  # displaced returned


def test_equip_rejects_missing_asset(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Tiara")])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_modifies == []  # never touched the character


def test_equip_modify_then_bucket_fails_reverts(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes(fail_closet_modify=True)
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    # modified to the new metadata, then reverted to the old on-chain URI
    assert f.char_modifies == [
        ("NFT7", "rUser", "https://cdn/new.json"),
        ("NFT7", "rUser", OLD_URL),
    ]
    # bucket untouched
    assets = {(slot, v): n for o, slot, v, n in es.read_closet_assets(conn)}
    assert assets == {("Head", "Crown"): 1}


def test_equip_bucket_fails_and_uri_undecodable_reports_honestly(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes(fail_closet_modify=True)
    rec = _char()
    rec.uri_hex = ""  # no decodable old URI to revert to
    s = ef.EquipSession(owner="rUser", character=rec, changes=[("Head", "Crown")])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    # only the forward modify happened; NO false revert was attempted
    assert f.char_modifies == [("NFT7", "rUser", "https://cdn/new.json")]
    record = json.loads((tmp_path / f"equip-{s.id}.json").read_text())
    assert record["status"] == "failed_revert"


def test_equip_revert_modify_not_landing_marks_failed_revert(tmp_path):
    """#184: the Closet swap fails (ledger did NOT commit) so the character is
    reverted — but the revert modify-back returns a FALSY hash (didn't land).
    The character may still carry the new traits while the Closet was not
    updated, so this is failed_revert (admin recovery), NOT the clean
    reverted_modify that a landed revert produces."""
    conn = _conn_with_bucket()
    f = _Fakes(fail_closet_modify=True, fail_revert_modify=True)
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    # forward modify happened, then a revert-back was ATTEMPTED (but didn't land)
    assert f.char_modifies == [
        ("NFT7", "rUser", "https://cdn/new.json"),
        ("NFT7", "rUser", OLD_URL),
    ]
    record = json.loads((tmp_path / f"equip-{s.id}.json").read_text())
    assert record["status"] == "failed_revert"  # NOT reverted_modify


# --- #107: phase-aware equip branches ---


def test_equip_mirror_failure_keeps_new_traits(tmp_path):
    """Character modify OK, Closet swap committed on-chain, only the DB mirror
    fails: reverting the character would strand the swapped Closet — the
    character must keep its new traits, the session ends DONE, and the journal
    records complete_pending_mirror."""
    conn, f = _conn_with_bucket(), _Fakes()
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert s.state == ef.DONE
    # exactly ONE character modify — no revert back to the old URI
    assert f.char_modifies == [("NFT7", "rUser", "https://cdn/new.json")]
    record = json.loads((tmp_path / f"equip-{s.id}.json").read_text())
    assert record["status"] == "complete_pending_mirror"
    assert record["sync_tx_hash"] == "MODHASH"
    assert record["mirror_pending"] is True


def test_equip_indeterminate_no_revert(tmp_path):
    """closet_modify raises (swap outcome unknown): fail-closed — FAILED, the
    character keeps its new URI (no revert), journal equip_sync_indeterminate."""
    conn, f = _conn_with_bucket(), _Fakes(raise_closet_modify=True)
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_modifies == [("NFT7", "rUser", "https://cdn/new.json")]  # no revert
    record = json.loads((tmp_path / f"equip-{s.id}.json").read_text())
    assert record["status"] == "equip_sync_indeterminate"
    # closet mirror untouched
    assets = {(slot, v): n for o, slot, v, n in es.read_closet_assets(conn)}
    assert assets == {("Head", "Crown"): 1}


# --- Batch equip: multiple slot changes in one save ---


def _conn_with_assets(pairs) -> sqlite3.Connection:
    """A Closet seeded with explicit (slot, value, count) rows."""
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    es.freeze_genesis(
        c, te.Genesis(trait_counts={}, edition_bodies={7: ("Straight Blue", "male")}), {}
    )
    es.set_closet_token(c, "rUser", "CLOSET", "00")
    es.set_closet_contents(c, "rUser", list(pairs), [])
    return c


def test_equip_batch_is_one_modify_and_one_sync(tmp_path):
    """Two slots in one batch: exactly one compose, one character modify, one
    Closet sync carrying BOTH deltas."""
    conn = _conn_with_assets([("Head", "Crown", 1), ("Eyes", "Laser", 1)])
    f = _Fakes()
    composed = []
    orig_compose = f.char_compose

    async def spy_compose(attrs, body, edition, rev):
        composed.append(attrs)
        return await orig_compose(attrs, body, edition, rev)

    f.char_compose = spy_compose
    s = ef.EquipSession(
        owner="rUser", character=_char(), changes=[("Head", "Crown"), ("Eyes", "Laser")]
    )
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    assert len(composed) == 1  # one compose for the whole batch
    assert f.char_modifies == [("NFT7", "rUser", "https://cdn/new.json")]  # one modify
    by_type = {a["trait_type"]: a["value"] for a in composed[0]}
    assert by_type["Head"] == "Crown" and by_type["Eyes"] == "Laser"
    assets = {(slot, v): n for o, slot, v, n in es.read_closet_assets(conn)}
    assert ("Head", "Crown") not in assets and ("Eyes", "Laser") not in assets
    assert assets[("Head", "None")] == 1 and assets[("Eyes", "None")] == 1
    assert s.displaced == {"Head": "None", "Eyes": "None"}


def test_equip_batch_aborts_whole_batch_on_a_bad_change(tmp_path):
    """The second change is not in the Closet: the batch is all-or-nothing, so
    the character is never modified and the Closet is untouched."""
    conn = _conn_with_assets([("Head", "Crown", 1)])
    f = _Fakes()
    s = ef.EquipSession(
        owner="rUser", character=_char(), changes=[("Head", "Crown"), ("Eyes", "Laser")]
    )
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_modifies == []  # never touched the character
    assets = {(slot, v): n for o, slot, v, n in es.read_closet_assets(conn)}
    assert assets == {("Head", "Crown"): 1}  # Closet untouched


def test_equip_batch_displaces_a_worn_trait_back_per_slot(tmp_path):
    """Closet assets are keyed (slot, value): a Crown displaced off Head returns
    as ('Head', 'Crown'), independent of any Eyes change in the same batch."""
    rec = _char()
    next(a for a in rec.attributes if a["trait_type"] == "Head")["value"] = "Crown"
    conn = _conn_with_assets([("Head", "Tiara", 1), ("Eyes", "Laser", 1)])
    f = _Fakes()
    s = ef.EquipSession(
        owner="rUser", character=rec, changes=[("Head", "Tiara"), ("Eyes", "Laser")]
    )
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    assert s.displaced == {"Head": "Crown", "Eyes": "None"}
    assets = {(slot, v): n for o, slot, v, n in es.read_closet_assets(conn)}
    assert assets[("Head", "Crown")] == 1  # displaced back into its own slot key
    assert assets[("Eyes", "None")] == 1
    assert ("Head", "Tiara") not in assets and ("Eyes", "Laser") not in assets


def test_equip_rejects_duplicate_slot_in_one_batch(tmp_path):
    conn = _conn_with_assets([("Head", "Crown", 1), ("Head", "Tiara", 1)])
    f = _Fakes()
    s = ef.EquipSession(
        owner="rUser", character=_char(), changes=[("Head", "Crown"), ("Head", "Tiara")]
    )
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert "duplicate slot" in (s.error or "")
    assert f.char_modifies == []


def test_equip_rejects_empty_batch(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_modifies == []


def test_equip_batch_journal_records_every_change(tmp_path):
    conn = _conn_with_assets([("Head", "Crown", 1), ("Eyes", "Laser", 1)])
    f = _Fakes()
    s = ef.EquipSession(
        owner="rUser", character=_char(), changes=[("Head", "Crown"), ("Eyes", "Laser")]
    )
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    record = json.loads((tmp_path / f"equip-{s.id}.json").read_text())
    assert record["status"] == "complete"
    assert record["op"] == "equip"
    assert record["changes"] == [
        {"slot": "Head", "incoming": "Crown", "displaced": "None"},
        {"slot": "Eyes", "incoming": "Laser", "displaced": "None"},
    ]
