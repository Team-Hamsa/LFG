# Tests for scripts/audit_trait_economy.py (economy report formatting).
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)
import audit_trait_economy as ate  # noqa: E402

from lfg_core import economy_store, nft_index, trait_economy  # noqa: E402


def test_economy_report_clean():
    cons = trait_economy.ConservationReport(trait_drift={}, ok=True)
    comp = trait_economy.CompletenessReport(orphan_bodies=[], slot_anomalies={}, ok=True)
    md = ate.format_economy_report(cons, comp, "mainnet", 3533, 3533, "2026-06-22T00-00-00Z")
    assert "Trait Economy Audit (mainnet)" in md
    assert "Conservation: **OK**" in md
    assert "Completeness: **OK**" in md


def test_economy_report_flags_drift():
    cons = trait_economy.ConservationReport(
        trait_drift={("Background", "Sky"): 1, ("Head", "Crown"): -1, ("Body", "S"): 1},
        ok=False,
    )
    comp = trait_economy.CompletenessReport(
        orphan_bodies=[9],
        slot_anomalies={3: ["Head"]},
        ok=False,
    )
    md = ate.format_economy_report(cons, comp, "mainnet", 100, 100, "2026-06-22T00-00-00Z")
    assert "Conservation: **DRIFT**" in md
    assert "Background" in md and "Sky" in md
    assert "Crown" in md
    assert "9" in md  # orphan body
    assert "Head" in md  # slot anomaly


def _rec(nft_id, edition, body_value, body_class, head, mutable=True):
    non_body = dict.fromkeys(trait_economy.NON_BODY_SLOTS, "None")
    non_body["Head"] = head
    attrs = [{"trait_type": "Body", "value": body_value}]
    attrs += [{"trait_type": s, "value": v} for s, v in non_body.items()]
    return nft_index.OnchainNft(
        nft_id=nft_id,
        nft_number=edition,
        owner="rUser",
        is_burned=False,
        mutable=mutable,
        uri_hex="",
        body=body_class,
        attributes=attrs,
        image="",
        ledger_index=1,
    )


def test_conservation_accepts_mixed_pre_and_post_model_history():
    """The auditor must report `ok` conservation over a DB that mixes a
    pre-model supply_changes row (e.g. a Trait Shop mint) with post-model
    blank-harvest state (a blank character whose 9 values live in its
    owner's Closet), against a frozen genesis."""
    conn = sqlite3.connect(":memory:")
    economy_store.init_economy_schema(conn)

    # Genesis: two dressed editions frozen as the conservation baseline.
    ed1 = _rec("NFT1", 1, "Straight Blue", "male", "Wizard Hat")
    ed2 = _rec("NFT2", 2, "Milady", "milady", "Antenna")
    genesis = trait_economy.build_genesis({1: ed1, 2: ed2})
    economy_store.freeze_genesis(conn, genesis, {"frozen_at": "test"})

    # Pre-model supply change: a Trait Shop mint grows supply by one loose
    # Hat trait token, with no associated character edition.
    economy_store.record_supply_change(
        conn,
        "mint",
        None,
        "",
        "",
        {"Head|Wizarding Cap": 1},
        "shop",
        "trait shop mint",
    )

    # Post-model blank-harvest state: edition 2 is stripped to blank in place
    # (no burn), and its former 9 slot values (8 non-body "None" + Head
    # "Antenna" + Body "Milady") sit in the owner's Closet as loose assets.
    ed2_blank = _rec("NFT2", 2, "None", "milady", "None")
    live = [ed1, ed2_blank]

    # Harvest moves ALL 9 of the blanked character's slot values into the
    # Closet — the 7 untouched "None" non-body slots too, not just Head.
    closet_assets = [("rUser", s, "None", 1) for s in trait_economy.NON_BODY_SLOTS if s != "Head"]
    closet_assets += [("rUser", "Head", "Antenna", 1), ("rUser", "Body", "Milady", 1)]
    trait_tokens = [("TRAIT_SHOP1", "rUser", "Head", "Wizarding Cap")]

    supply_changes = economy_store.read_supply_changes(conn)
    live_max = max(r.nft_number for r in live if r.nft_number is not None)
    max_edition = max(trait_economy.effective_max_edition(genesis, supply_changes), live_max)
    canonical, _ = trait_economy.dedupe_editions(live, max_edition)
    census = trait_economy.asset_census(canonical, closet_assets, trait_tokens)

    conservation = trait_economy.verify_conservation(genesis, census, supply_changes)
    assert conservation.ok, f"unexpected drift: {conservation.trait_drift}"


def test_completeness_ok_for_blank_and_rebodied_characters():
    """Blank-model completeness must NOT flag:
    - a fully blank character (all slots "None") — skipped entirely, and
    - a dressed character now wearing a DIFFERENT body value than its genesis
      body (bodies are swappable; the retired `wrong_body` check must not fire).
    """
    # Genesis: two dressed editions with their frozen bodies.
    ed1 = _rec("NFT1", 1, "Straight Blue", "male", "Wizard Hat")
    ed2 = _rec("NFT2", 2, "Milady", "milady", "Antenna")
    genesis = trait_economy.build_genesis({1: ed1, 2: ed2})

    # Edition 1: harvested to a blank in place. Edition 2: re-dressed onto a
    # DIFFERENT body value ("Straight Blue") than its genesis body ("Milady").
    ed1_blank = _rec("NFT1", 1, "None", "male", "None")
    ed2_rebodied = _rec("NFT2", 2, "Straight Blue", "male", "Antenna")
    canonical = {1: ed1_blank, 2: ed2_rebodied}

    comp = trait_economy.verify_completeness(canonical, genesis)
    assert comp.ok, f"unexpected completeness violation: {comp}"
    assert comp.orphan_bodies == []
    assert comp.slot_anomalies == {}


def test_completeness_main_ok_over_blank_and_rebodied_db(tmp_path, monkeypatch, capsys):
    """The auditor's main() exits 0 (clean) over a DB holding a blank character
    and a re-bodied character, so scripts/audit_trait_economy.py no longer
    false-flags the blank model."""
    ed1 = _rec("NFT1", 1, "Straight Blue", "male", "Wizard Hat")
    ed2 = _rec("NFT2", 2, "Milady", "milady", "Antenna")
    genesis = trait_economy.build_genesis({1: ed1, 2: ed2})

    db_path = str(tmp_path / "onchain_testnet.db")
    conn = nft_index.init_db(db_path)
    economy_store.init_economy_schema(conn)
    economy_store.freeze_genesis(conn, genesis, {"frozen_at": "test"})

    # Live: edition 1 blanked in place; edition 2 re-dressed onto a different body.
    # Their harvested/swapped-out assets sit in the Closet so conservation holds.
    ed1_blank = _rec("NFT1", 1, "None", "male", "None")
    ed2_rebodied = _rec("NFT2", 2, "Straight Blue", "male", "Antenna")
    for rec in (ed1_blank, ed2_rebodied):
        nft_index.upsert(conn, rec)

    # Edition 1 fully harvested → its 8 non-body values into the Closet. Its
    # harvested body (Straight Blue) is re-worn by edition 2 below, so it does
    # NOT sit loose in the Closet.
    closet = [(s, "None", 1) for s in trait_economy.NON_BODY_SLOTS if s != "Head"]
    closet += [("Head", "Wizard Hat", 1)]
    # Edition 2 swapped its body out (Milady → Straight Blue): the displaced
    # Milady body lands in the Closet.
    closet += [("Body", "Milady", 1)]
    economy_store.set_closet_contents(conn, "rUser", closet, [])
    conn.commit()
    conn.close()

    monkeypatch.setattr(nft_index, "index_db_path", lambda network: db_path)
    monkeypatch.setattr(
        ate.sys, "argv", ["audit", "--network", "testnet", "--report-dir", str(tmp_path)]
    )
    rc = ate.main()
    out = capsys.readouterr().out
    assert rc == 0, f"expected clean audit, got rc={rc}\n{out}"
    assert "Completeness: OK" in out
