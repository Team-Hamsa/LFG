# Assemble flow: dress a caller-owned BLANK character in place via
# NFTokenModify — no mint, no offer, no accept. Driven through injected
# fakes — no network.

import asyncio
import json
import sqlite3

from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from lfg_core import nft_index
from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft
from tests.economy_helpers import flaky_mirror_conn

NON_BODY = te.NON_BODY_SLOTS


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _blank_char(
    edition: int = 7, mutable: bool = True, uri_hex: str = "AABB", body_value: str = "None"
) -> OnchainNft:
    """A blank character: every slot, including Body, reads 'None' (unless a
    caller passes a non-blank body_value to exercise the precheck reject)."""
    attrs = [{"trait_type": "Body", "value": body_value}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return OnchainNft(
        nft_id=f"NFT{edition}",
        nft_number=edition,
        owner="rUser",
        is_burned=False,
        mutable=mutable,
        uri_hex=uri_hex,
        body="male",
        attributes=attrs,
        image="",
        ledger_index=1,
    )


def _conn_with_closet(body_value: str = "Straight Blue") -> sqlite3.Connection:
    """A closet stocked with one Body + a full 'None' set for the owner."""
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    # The on-chain index shares the per-network DB (deps.conn is one shared
    # connection); assemble's post-success stamp writes onchain_nfts, so the
    # fixture must carry that schema too (mirrors the equip test fixture).
    c.executescript(nft_index._SCHEMA)
    es.set_closet_token(c, "rUser", "CLOSET", "00")
    contents = [(s, "None", 1) for s in NON_BODY] + [("Body", body_value, 1)]
    es.set_closet_contents(c, "rUser", contents, [])
    return c


class _Fakes:
    def __init__(
        self,
        *,
        fail_closet_modify: bool = False,
        raise_closet_modify: bool = False,
        fail_char_modify: bool = False,
        fail_char_revert_modify: bool = False,
    ) -> None:
        self.fail_closet_modify = fail_closet_modify
        self.raise_closet_modify = raise_closet_modify
        self.fail_char_modify = fail_char_modify
        self.fail_char_revert_modify = fail_char_revert_modify
        self.char_modifies: list[tuple[str, str, str]] = []
        self.bucket_modifies = 0
        self.mints: list[str] = []
        self.offers: list[tuple[str, str]] = []
        self.closet_owner_addr: str | None = "rUser"

    async def closet_owner(self, nft_id: str) -> str | None:
        return self.closet_owner_addr

    async def closet_upload(self, meta: dict) -> str:
        return "https://cdn/b.json"

    async def closet_mint(self, url: str):
        return "CLOSET"

    async def closet_offer(self, nft_id, owner):
        return "O"

    async def closet_accept(self, offer_id):
        return {"xumm_url": "x"}

    async def closet_modify(self, nft_id, owner, url):
        if self.raise_closet_modify:
            raise RuntimeError("timeout after submit")
        if self.fail_closet_modify:
            return None
        self.bucket_modifies += 1
        return "MODHASH"

    async def char_compose(self, attrs, body, edition, rev):
        return ("img", "vid.mp4", "meta")

    async def char_mint(self, url: str):
        self.mints.append(url)
        return "MINTED"

    async def char_modify(self, nft_id, owner, url):
        self.char_modifies.append((nft_id, owner, url))
        if len(self.char_modifies) == 1:
            return None if self.fail_char_modify else "MODIFYHASH"
        # A second call is always the revert-to-blank attempt.
        return None if self.fail_char_revert_modify else "MODIFYHASH"

    async def char_burn(self, nft_id, owner):
        return "BURN"

    async def char_offer(self, nft_id, owner):
        self.offers.append((nft_id, owner))
        return "OFFER"

    async def char_accept(self, offer_id):
        return {"xumm_url": "accept"}


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
        closet_owner_fn=f.closet_owner,
        records_dir=str(records_dir),
    )


def _session(character: OnchainNft | None = None) -> ef.AssembleSession:
    return ef.AssembleSession(
        owner="rUser",
        character=character or _blank_char(),
        body_value="Straight Blue",
        body_class="male",
        chosen=dict.fromkeys(NON_BODY, "None"),
    )


def _all_slot_assets(conn) -> dict[tuple[str, str], int]:
    return {(s, v): n for o, s, v, n in es.read_closet_assets(conn)}


# --- Happy path ---


def test_assemble_happy_path_modifies_and_debits_closet(tmp_path):
    conn, f = _conn_with_closet(), _Fakes()
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    assert f.char_modifies == [("NFT7", "rUser", "meta")]
    assert f.mints == []  # no mint
    assert f.offers == []  # no offer
    assert s.results[0]["nft_id"] == "NFT7"  # the EXISTING character nft_id
    assert s.results[0]["accept"] is None
    assert s.results[0]["image_url"] == "img"
    assert s.results[0]["video_url"] == "vid.mp4"
    # closet fully debited: Body + every chosen "None" value
    assert _all_slot_assets(conn) == {}
    assert es.read_supply_changes(conn) == []  # no supply_changes writes
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "complete"
    assert record["nft_id"] == "NFT7"


# --- Post-save on-chain index stamp: the GO grid thumbnail reads
# onchain_nfts.image, so a successful assemble must stamp the freshly composed
# art there right away rather than racing the listener (the stale-thumbnail
# fix). ---


def test_assemble_success_stamps_index_with_new_art(tmp_path):
    conn, f = _conn_with_closet(), _Fakes()
    # Seed the pre-assemble BLANK row so we can prove it flips to the new art,
    # not merely that a row appears.
    rec = _blank_char()
    rec.body = "skeleton"
    nft_index.upsert(
        conn,
        OnchainNft(
            nft_id=rec.nft_id,
            nft_number=rec.nft_number,
            owner=rec.owner,
            is_burned=False,
            mutable=rec.mutable,
            uri_hex=rec.uri_hex,
            body=rec.body,
            attributes=rec.attributes,
            image=ef.config.BLANK_IMAGE_URL,
            ledger_index=1,
        ),
    )
    s = _session(character=rec)
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM onchain_nfts WHERE nft_id=?", ("NFT7",)).fetchone()
    assert row is not None
    by_type = {a["trait_type"]: a["value"] for a in json.loads(row["attributes_json"])}
    assert by_type["Body"] == "Straight Blue"  # dressed, not blank
    assert row["image"] == "img"  # the composed art (char_compose -> "img")
    assert row["image"] != ef.config.BLANK_IMAGE_URL
    assert row["uri_hex"] == b"meta".hex()  # composed metadata URL
    assert row["owner"] == "rUser"  # an in-place modify never moves ownership
    assert row["is_burned"] == 0
    assert row["body"] == "male"


def test_assemble_mirror_failure_still_stamps_index_with_new_art(tmp_path):
    """The complete_pending_mirror branch (Closet debit COMMITTED on-chain,
    only the DB mirror failed) still ends DONE with the character dressed — the
    index stamp must fire on THIS success path too."""
    conn, f = _conn_with_closet(), _Fakes()
    rec = _blank_char()
    rec.body = "skeleton"
    s = _session(character=rec)
    _run(ef.run_assemble(s, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert s.state == ef.DONE
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "complete_pending_mirror"

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM onchain_nfts WHERE nft_id=?", ("NFT7",)).fetchone()
    assert row is not None
    by_type = {a["trait_type"]: a["value"] for a in json.loads(row["attributes_json"])}
    assert by_type["Body"] == "Straight Blue"
    assert row["image"] == "img"
    assert row["body"] == "male"


# --- Precheck rejections ---


def test_assemble_rejects_non_blank_target(tmp_path):
    conn, f = _conn_with_closet(), _Fakes()
    # A non-blank character (Body already carries a real value) fails
    # can_assemble's "character is not blank" check.
    s = _session(character=_blank_char(body_value="Straight Blue"))
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert "not blank" in (s.error or "").lower()
    assert f.char_modifies == []
    assert _all_slot_assets(conn) != {}  # closet untouched


def test_assemble_rejects_missing_body_asset(tmp_path):
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    es.set_closet_token(conn, "rUser", "CLOSET", "00")
    # Closet has the full non-body set but NOT the Body asset.
    es.set_closet_contents(conn, "rUser", [(s, "None", 1) for s in NON_BODY], [])
    f = _Fakes()
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert "cannot assemble" in (s.error or "").lower()
    assert f.char_modifies == []
    assert _all_slot_assets(conn) == {(s2, "None"): 1 for s2 in NON_BODY}  # untouched


# --- Ledger-fail after modify: revert to the blank URI ---


def test_assemble_closet_fail_reverts_character_to_blank(tmp_path):
    conn, f = _conn_with_closet(), _Fakes(fail_closet_modify=True)
    old_uri_hex = b"ipfs://blank".hex()
    s = _session(character=_blank_char(uri_hex=old_uri_hex))
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    # First call dresses the character, second call reverts to the blank URI.
    assert f.char_modifies[0] == ("NFT7", "rUser", "meta")
    assert f.char_modifies[1] == ("NFT7", "rUser", "ipfs://blank")
    assert _all_slot_assets(conn) != {}  # closet untouched (still has the assets)
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "reverted_modify"


def test_assemble_closet_fail_and_revert_fail_is_failed_revert(tmp_path):
    conn, f = _conn_with_closet(), _Fakes(fail_closet_modify=True, fail_char_revert_modify=True)
    s = _session(character=_blank_char(uri_hex=b"ipfs://blank".hex()))
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "failed_revert"


# --- #107 phase-aware branches ---


def test_assemble_mirror_failure_completes_pending_mirror_no_revert(tmp_path):
    """The Closet debit committed on-chain; only the DB mirror write fails:
    DONE, mirror_pending set, no revert of the character."""
    conn, f = _conn_with_closet(), _Fakes()
    s = _session()
    _run(ef.run_assemble(s, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert s.state == ef.DONE
    assert len(f.char_modifies) == 1  # only the dress modify; NO revert modify
    assert s.results and s.results[0]["nft_id"] == "NFT7"
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "complete_pending_mirror"
    assert record["sync_tx_hash"] == "MODHASH"
    assert record["mirror_pending"] is True


def test_assemble_indeterminate_sync_fails_no_revert(tmp_path):
    """closet_modify raises (commit status unknown): fail-closed — FAILED, no
    compensation, journal assemble_sync_indeterminate."""
    conn, f = _conn_with_closet(), _Fakes(raise_closet_modify=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert len(f.char_modifies) == 1  # the dress modify happened; no revert
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "assemble_sync_indeterminate"
    assert record["sync_tx_hash"] is None
    # DB mirror untouched
    assert _all_slot_assets(conn) != {}


# --- Preconditions (mutable gating; Closet requirement) ---


def test_assemble_rejects_non_mutable_target(tmp_path):
    conn, f = _conn_with_closet(), _Fakes()
    s = _session(character=_blank_char(mutable=False))
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_modifies == []
    assert _all_slot_assets(conn) != {}


def test_assemble_rejected_without_active_closet(tmp_path):
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    # no closet row at all -> status none
    f = _Fakes()
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_modifies == []
    assert "closet" in (s.error or "").lower()


def test_assemble_rejected_when_active_closet_gone_onledger(tmp_path):
    conn, f = _conn_with_closet(), _Fakes()
    f.closet_owner_addr = None  # token no longer owned by the user on-ledger
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_modifies == []


def test_assemble_char_modify_returns_falsy_fails_cleanly(tmp_path):
    conn, f = _conn_with_closet(), _Fakes(fail_char_modify=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "failed_modify"
    assert _all_slot_assets(conn) != {}  # closet untouched
