# scripts/audit_trait_economy.py end-to-end: a supply_changes ledger row lets a
# legitimately-grown edition pass conservation; remove it and it reads as drift.

import os
import sys

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import audit_trait_economy as ate  # noqa: E402

from lfg_core import economy_store as es  # noqa: E402
from lfg_core import nft_index, trait_economy  # noqa: E402

NON_BODY = trait_economy.NON_BODY_SLOTS


def _char(edition: int) -> nft_index.OnchainNft:
    attrs = [{"trait_type": "Body", "value": "Straight Blue"}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return nft_index.OnchainNft(
        nft_id=f"NFT{edition}",
        nft_number=edition,
        owner="rUser",
        is_burned=False,
        mutable=True,
        uri_hex="",
        body="male",
        attributes=attrs,
        image="",
        ledger_index=edition,
    )


def _setup_db(path: str) -> None:
    conn = nft_index.init_db(path)
    es.init_economy_schema(conn)
    # Live characters: edition 1 (genesis) + edition 3536 (grown via ledger).
    nft_index.upsert(conn, _char(1))
    nft_index.upsert(conn, _char(3536))
    genesis = trait_economy.Genesis(
        trait_counts={(s, "None"): 1 for s in NON_BODY},
        edition_bodies={1: ("Straight Blue", "male")},
    )
    es.freeze_genesis(conn, genesis, {"max_edition": "3535"})
    es.record_supply_change(
        conn,
        "mint",
        3536,
        "Straight Blue",
        "male",
        {f"{s}|None": 1 for s in NON_BODY},
        "test",
        "grew beyond 3535",
    )
    conn.close()


def _run_audit(tmp_path, monkeypatch) -> int:
    db = str(tmp_path / "onchain_testnet.db")
    monkeypatch.setenv("ONCHAIN_DB_PATH", db)
    if not os.path.isfile(db):
        _setup_db(db)
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_trait_economy.py", "--network", "testnet", "--report-dir", str(tmp_path / "r")],
    )
    return ate.main()


def test_logged_growth_passes(tmp_path, monkeypatch):
    assert _run_audit(tmp_path, monkeypatch) == 0  # ledger explains edition 3536


def test_unlogged_growth_is_drift(tmp_path, monkeypatch):
    db = str(tmp_path / "onchain_testnet.db")
    _setup_db(db)
    conn = nft_index.init_db(db)
    conn.execute("DELETE FROM supply_changes")
    conn.commit()
    conn.close()
    assert _run_audit(tmp_path, monkeypatch) == 1  # now edition 3536 is unexplained drift


def test_issuer_keyed_closet_row_fails_the_audit(tmp_path, monkeypatch):
    """#383: the nightly audit is what notices a Closet keyed to a project
    signing account, so a recurrence cannot sit unseen for days again."""
    from lfg_core import config

    db = str(tmp_path / "onchain_testnet.db")
    _setup_db(db)
    conn = nft_index.init_db(db)
    conn.execute("DROP INDEX IF EXISTS idx_closet_tokens_nft_id")
    conn.execute(
        "INSERT INTO closet_tokens (owner, nft_id, uri_hex, status) VALUES (?,?,?,?)",
        (config.SWAP_ISSUER_ADDRESS, "CLOSET1", "AB", "pending_accept"),
    )
    conn.commit()
    conn.close()

    assert _run_audit(tmp_path, monkeypatch) == 1

    report = next((tmp_path / "r").glob("trait-economy-audit-*.md")).read_text()
    assert "Closet ownership: **ANOMALIES**" in report
    assert "project-account row" in report


def test_clean_closet_ownership_does_not_fail_the_audit(tmp_path, monkeypatch):
    db = str(tmp_path / "onchain_testnet.db")
    _setup_db(db)
    conn = nft_index.init_db(db)
    conn.execute(
        "INSERT INTO closet_tokens (owner, nft_id, uri_hex, status) VALUES (?,?,?,?)",
        ("rRealUser", "CLOSET1", "AB", "active"),
    )
    conn.commit()
    conn.close()

    assert _run_audit(tmp_path, monkeypatch) == 0


# --- #493: a drift table of only net-zero-per-slot swap substitution is clean ---


def _swap_edition_1_head(db: str) -> None:
    """Edition 1's Head went None -> Crown with no supply row: a trait swap,
    which substitutes one value for another within the slot (Head|None -1,
    Head|Crown +1) and so nets to zero there."""
    conn = nft_index.init_db(db)
    rec = _char(1)
    rec.attributes = [
        {**a, "value": "Crown"} if a["trait_type"] == "Head" else a for a in rec.attributes
    ]
    nft_index.upsert(conn, rec)
    conn.commit()
    conn.close()


def _run_with_webhook(tmp_path, monkeypatch) -> tuple[int, list[str]]:
    posted: list[str] = []
    monkeypatch.setattr(ate, "post_alert", lambda url, body: posted.append(body) or True)
    db = str(tmp_path / "onchain_testnet.db")
    monkeypatch.setenv("ONCHAIN_DB_PATH", db)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit_trait_economy.py",
            "--network",
            "testnet",
            "--report-dir",
            str(tmp_path / "r"),
            "--alert-webhook",
            "https://discord.invalid/webhook",
        ],
    )
    return ate.main(), posted


def test_benign_only_drift_reports_clean_and_does_not_alert(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "onchain_testnet.db")
    _setup_db(db)
    _swap_edition_1_head(db)

    rc, posted = _run_with_webhook(tmp_path, monkeypatch)
    out = capsys.readouterr().out

    assert rc == 0, out
    assert posted == []
    assert "Conservation: OK (2 benign swap-substitution rows, net zero per slot)" in out
    (report,) = os.listdir(tmp_path / "r")
    text = open(tmp_path / "r" / report).read()
    assert "- Conservation: **OK** (2 benign swap-substitution rows, net zero per slot)" in text
    assert "| Head | Crown | +1 |" in text  # the rows stay in the report for review


def test_real_drift_still_fails_and_alerts(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "onchain_testnet.db")
    _setup_db(db)
    _swap_edition_1_head(db)  # benign rows alongside the real one
    conn = nft_index.init_db(db)
    es.init_economy_schema(conn)
    es.set_closet_contents(conn, "rUser", [("Eyes", "Laser", 1)], [])  # created from nothing
    conn.close()

    rc, posted = _run_with_webhook(tmp_path, monkeypatch)
    out = capsys.readouterr().out

    assert rc == 1, out
    assert "Conservation: DRIFT" in out
    assert len(posted) == 1 and posted[0].startswith("**Trait economy audit: DRIFT**")
    # Actionable without opening a DB: network + the report file's own path.
    (report,) = os.listdir(tmp_path / "r")
    report_path = os.path.join(str(tmp_path / "r"), report)
    assert "testnet" in posted[0]
    assert f"Report: {report_path}" in posted[0]


def test_post_alert_never_raises_even_when_the_webhook_post_fails(monkeypatch):
    """The shared alert helper's contract (scripts/_alerts.py): a webhook POST
    that raises must never propagate — an audit's exit code can never depend
    on whether Discord happens to be reachable. `ate.post_alert` is the SAME
    function object every audit script imports, so this exercises the one
    shared implementation directly rather than each caller."""
    import urllib.request

    def boom(*args, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert ate.post_alert("https://discord.invalid/webhook", "body") is False


def test_benign_drift_with_a_completeness_violation_alerts_as_violations():
    """The run is non-clean because of completeness, not conservation: the
    alert must not headline benign substitution as DRIFT."""
    report = trait_economy.ConservationReport(
        trait_drift={("Head", "None"): -1, ("Head", "Crown"): +1}, ok=False
    )
    body = ate.build_alert_body(
        "testnet",
        2,
        report,
        trait_economy.CompletenessReport(orphan_bodies=[9], slot_anomalies={}, ok=False),
        "reports/x.md",
    )
    assert body.startswith("**Trait economy audit: VIOLATIONS**")
    assert "benign swap substitution" in body
