# Assemble flow: body + full set from the Closet -> mint the edition + offer.
# Driven through injected fakes — no network.

import asyncio
import json
import logging
import sqlite3

from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from lfg_core import trait_economy as te
from tests.economy_helpers import flaky_mirror_conn

NON_BODY = te.NON_BODY_SLOTS


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _conn_with_bucket(edition: int = 7) -> sqlite3.Connection:
    """Genesis with one edition; the owner's bucket holds that body + a full
    'None' asset set + an existing bucket token."""
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    genesis = te.Genesis(
        trait_counts={(s, "None"): 1 for s in NON_BODY},
        edition_bodies={edition: ("Straight Blue", "male")},
    )
    es.freeze_genesis(c, genesis, {})
    es.set_closet_token(c, "rUser", "CLOSET", "00")
    es.set_closet_contents(c, "rUser", [(s, "None", 1) for s in NON_BODY], [edition])
    return c


class _Fakes:
    def __init__(
        self,
        *,
        fail_closet_modify=False,
        raise_closet_modify=False,
        fail_offer=False,
        raise_offer=False,
        fail_char_burn=False,
        fail_accept=False,
    ) -> None:
        self.fail_closet_modify = fail_closet_modify
        self.raise_closet_modify = raise_closet_modify
        self.fail_offer = fail_offer
        self.raise_offer = raise_offer
        self.fail_char_burn = fail_char_burn
        self.fail_accept = fail_accept
        self.mints: list[str] = []
        self.char_burns: list[tuple[str, str]] = []
        self.bucket_modifies = 0
        # address returned by closet_owner; None means not yet owned by user.
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
        return "CHAR7"

    async def char_modify(self, nft_id, owner, url):
        return "H"

    async def char_burn(self, nft_id, owner):
        self.char_burns.append((nft_id, owner))
        return None if self.fail_char_burn else "BURN"

    async def char_offer(self, nft_id, owner):
        if self.raise_offer:
            raise RuntimeError("offer submit blew up")
        return None if self.fail_offer else "OFFER"

    async def char_accept(self, offer_id):
        return None if self.fail_accept else {"xumm_url": "accept"}


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


def _session() -> ef.AssembleSession:
    return ef.AssembleSession(
        owner="rUser",
        edition=7,
        chosen=dict.fromkeys(NON_BODY, "None"),
        body_value="Straight Blue",
        body_class="male",
        live_editions=set(),
    )


def test_assemble_happy_path(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    assert s.new_nft_id == "CHAR7"
    assert f.bucket_modifies == 1
    # bucket fully drained
    assert es.read_closet_bodies(conn) == []
    assert es.read_closet_assets(conn) == []
    assert s.results[0]["accept"] == {"xumm_url": "accept"}
    # #250: compose's outputs thread into the result — an animated assemble's
    # hero plays from video_url, so dropping/renaming the key must fail here.
    assert s.results[0]["image_url"] == "img"
    assert s.results[0]["video_url"] == "vid.mp4"


def test_assemble_rejects_incomplete_set(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = _session()
    del s.chosen[NON_BODY[0]]  # missing a slot
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.mints == []  # never minted
    assert es.read_closet_bodies(conn) == [("rUser", 7)]  # bucket untouched


def test_assemble_mint_then_drain_fails_reverts(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes(fail_closet_modify=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.mints == ["meta"]  # minted...
    assert f.char_burns == [("CHAR7", "")]  # ...then burned back (issuer-held)
    assert s.new_nft_id is None
    assert es.read_closet_bodies(conn) == [("rUser", 7)]  # bucket untouched


def test_assemble_drain_fail_then_burnback_fail_keeps_nft_id(tmp_path):
    # Mint succeeds, bucket drain fails, AND the compensating burn-back fails:
    # the minted token's id MUST be retained in the journal for admin recovery.
    conn, f = _conn_with_bucket(), _Fakes(fail_closet_modify=True, fail_char_burn=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert s.new_nft_id == "CHAR7"  # NOT wiped — token is stranded, id preserved
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "failed_revert_mint"
    assert record["new_nft_id"] == "CHAR7"


def test_assemble_offer_fail_parks_token(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes(fail_offer=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert s.new_nft_id == "CHAR7"  # token exists, parked for re-offer
    assert f.char_burns == []  # NOT burned — bucket already drained, no asset loss
    assert es.read_closet_bodies(conn) == []  # drained
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "minted_no_offer"


def test_assemble_accept_payload_failure_warns_but_completes(tmp_path, caplog):
    """#262 'warn, don't fail': only the XUMM accept-payload build failed
    (429 backoff / outage) — the delivery offer is already on-chain and
    claimable via Xaman Events, so the session must complete DONE with a
    warning breadcrumb in the ops log, never FAILED."""
    conn, f = _conn_with_bucket(), _Fakes(fail_accept=True)
    s = _session()
    with caplog.at_level(logging.WARNING):
        _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    assert s.results[0]["accept"] is None
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "complete"
    assert any("accept payload creation failed" in r.message for r in caplog.records)


# --- #107: phase-aware assemble branches ---


def test_assemble_mirror_failure_does_not_burn_and_delivers(tmp_path):
    """Mint OK, Closet drain committed on-chain, only the DB mirror fails:
    the mint must NOT be burned back (the Closet is drained on-chain — burning
    would destroy the user's body + assets). Delivery continues; the session
    ends DONE with a complete_pending_mirror journal."""
    conn, f = _conn_with_bucket(), _Fakes()
    s = _session()
    _run(ef.run_assemble(s, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert s.state == ef.DONE
    assert f.char_burns == []  # NO destructive compensation
    assert s.new_nft_id == "CHAR7"
    assert s.results and s.results[0]["accept"] == {"xumm_url": "accept"}
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "complete_pending_mirror"
    assert record["sync_tx_hash"] == "MODHASH"
    assert record["mirror_pending"] is True


def test_assemble_indeterminate_keeps_mint_no_burn(tmp_path):
    """closet_modify raises (drain outcome unknown): fail-closed — FAILED, the
    mint is kept (its id journaled), no burn-back against an unknown Closet."""
    conn, f = _conn_with_bucket(), _Fakes(raise_closet_modify=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_burns == []  # no compensation while state is unknown
    assert s.new_nft_id == "CHAR7"
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "assemble_sync_indeterminate"
    assert record["new_nft_id"] == "CHAR7"


def test_assemble_mirror_fail_then_offer_fail_precedence(tmp_path):
    """Mirror fails AND the later offer step fails: the later step's status
    (minted_no_offer) wins the status field, but the pending-mirror fact is
    kept via mirror_pending + sync_tx_hash. No burn either way."""
    conn, f = _conn_with_bucket(), _Fakes(fail_offer=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_burns == []
    assert s.new_nft_id == "CHAR7"  # parked for re-offer
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "minted_no_offer"
    assert record["mirror_pending"] is True
    assert record["sync_tx_hash"] == "MODHASH"


def test_assemble_rejected_without_active_closet(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    # Remove the closet token seeded by _conn_with_bucket so there is no record.
    conn.execute("DELETE FROM closet_tokens WHERE owner = 'rUser'")
    conn.commit()
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.FAILED
    assert f.mints == []  # never minted
    assert "closet" in (s.error or "").lower()


def test_assemble_succeeds_with_active_closet(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    # closet is already seeded via _conn_with_bucket; closet_owner_fn promotes it
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.DONE
    assert s.new_nft_id == "CHAR7"


def test_assemble_mirror_fail_then_offer_raise_keeps_mirror_journal(tmp_path):
    """Greptile #151: the drain COMMITTED (mirror-fail sets the sticky fields in
    memory) and then the delivery offer RAISES (not returns-falsy). The outer
    handler must persist a terminal journal carrying mirror_pending +
    sync_tx_hash — otherwise the on-disk record stays at the pre-drain
    'minted' checkpoint and recovery burns the mint back against an
    already-drained Closet."""
    conn, f = _conn_with_bucket(), _Fakes(raise_offer=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_burns == []  # still no destructive compensation
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "failed"
    assert record["mirror_pending"] is True
    assert record["sync_tx_hash"] == "MODHASH"


def test_assemble_journal_checkpoints_closet_synced_before_offer(tmp_path):
    """CodeRabbit #151: the drain committed; a process CRASH during offer
    delivery (no exception for the outer handler to catch) must not leave the
    on-disk journal at the pre-drain 'minted' checkpoint — an admin following
    it would burn the mint back against an already-drained Closet. A
    'closet_synced' record carrying sync_tx_hash must be durable BEFORE the
    offer call."""
    import dataclasses

    conn, f = _conn_with_bucket(), _Fakes()
    s = _session()
    deps = _deps(conn, f, tmp_path)
    at_offer: dict = {}

    async def spy_offer(nft_id, owner):
        at_offer.update(json.loads((tmp_path / f"assemble-{s.id}.json").read_text()))
        return await f.char_offer(nft_id, owner)

    _run(ef.run_assemble(s, dataclasses.replace(deps, char_offer_fn=spy_offer)))

    assert s.state == ef.DONE
    assert at_offer["status"] == "closet_synced"
    assert at_offer["sync_tx_hash"] == "MODHASH"
    assert at_offer["mirror_pending"] is False


def test_assemble_mirror_fail_checkpoint_carries_sticky_fields(tmp_path):
    """Same checkpoint on the ClosetMirrorError path: the drain committed
    on-chain, only the DB mirror failed — the pre-offer record must already
    carry mirror_pending + the committed tx hash."""
    import dataclasses

    conn, f = _conn_with_bucket(), _Fakes()
    s = _session()
    deps = _deps(flaky_mirror_conn(conn), f, tmp_path)
    at_offer: dict = {}

    async def spy_offer(nft_id, owner):
        at_offer.update(json.loads((tmp_path / f"assemble-{s.id}.json").read_text()))
        return await f.char_offer(nft_id, owner)

    _run(ef.run_assemble(s, dataclasses.replace(deps, char_offer_fn=spy_offer)))

    assert s.state == ef.DONE
    assert at_offer["status"] == "closet_synced"
    assert at_offer["sync_tx_hash"] == "MODHASH"
    assert at_offer["mirror_pending"] is True
