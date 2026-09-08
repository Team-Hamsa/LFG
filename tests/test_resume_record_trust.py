# 2026-09-08 incident: fixture burn-to-mint records (network=testnet, wallet
# rUSERUSER…) leaked into the prod checkout by a test whose monkeypatch.undo()
# reverted the autouse JOBS_DIR pins. The prod service resumed them at boot
# and minted 32 real mainnet editions to an undeliverable wallet. Three
# defences, each tested here:
#   1. the suite pins both record dirs to a temp dir (conftest) — no test can
#      write a record the live service would find;
#   2. bulk_mint_flow.load_all_resumable refuses (and durably fails) any record
#      for another network or an invalid wallet, so it is never launched;
#   3. burn2mint_flow.load_all_resumable does the same for sessions.
import json
import os

from lfg_core import bulk_mint_flow, burn2mint_flow, config

VALID = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"


def test_suite_pins_record_dirs_away_from_the_checkout():
    for mod, default in ((bulk_mint_flow, "bulk_mint_jobs"), (burn2mint_flow, "burn2mint_jobs")):
        assert mod.JOBS_DIR != default
        assert os.path.isabs(mod.JOBS_DIR)
        assert "lfg-test-jobs-" in mod.JOBS_DIR


def test_record_refusal_reasons(monkeypatch):
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")
    assert bulk_mint_flow.record_refusal("mainnet", VALID) is None
    assert "network" in bulk_mint_flow.record_refusal("testnet", VALID)
    assert "wallet" in bulk_mint_flow.record_refusal("mainnet", "rUSERUSERUSERUSERUSERUSERUSERUSER")
    assert "wallet" in bulk_mint_flow.record_refusal("mainnet", "")


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _bulk_record(job_id, *, network, wallet, state):
    j = bulk_mint_flow.BulkMintJob(
        discord_id="u1", wallet_address=wallet, requested_qty=1, platform="discord"
    )
    j.id = job_id
    j.network = network
    j.state = state
    return j.serialize()  # the on-disk record shape persist() writes, not the API to_dict()


def test_bulk_loader_refuses_and_fails_untrusted_records(tmp_path, monkeypatch):
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")
    _write(
        str(tmp_path / "good.json"),
        _bulk_record("good", network="mainnet", wallet=VALID, state=bulk_mint_flow.PAID),
    )
    _write(
        str(tmp_path / "othernet.json"),
        _bulk_record("othernet", network="testnet", wallet=VALID, state=bulk_mint_flow.PAID),
    )
    _write(
        str(tmp_path / "badwallet.json"),
        _bulk_record(
            "badwallet",
            network="mainnet",
            wallet="rUSERUSERUSERUSERUSERUSERUSERUSER",
            state=bulk_mint_flow.FULFILLING,
        ),
    )

    resumed = bulk_mint_flow.load_all_resumable()
    assert [j.id for j in resumed] == ["good"]
    for name in ("othernet", "badwallet"):
        with open(tmp_path / f"{name}.json") as f:
            rec = json.load(f)
        assert rec["state"] == bulk_mint_flow.FAILED
        assert rec["error"].startswith("refused at resume:")
    # durable: a second boot does not see them either
    assert [j.id for j in bulk_mint_flow.load_all_resumable()] == ["good"]


def test_burn2mint_loader_refuses_and_fails_untrusted_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(burn2mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")

    def rec(sid, *, network, wallet):
        s = burn2mint_flow.Burn2MintSession(
            discord_id="u1", wallet_address=wallet, nft_ids=["A"], platform="discord"
        )
        s.id = sid
        s.network = network
        s.state = burn2mint_flow.AWAITING_BURNS
        s.burns[0].state = burn2mint_flow.B_BURNED
        return s.serialize()  # on-disk record shape, not the API to_dict()

    _write(str(tmp_path / "good.json"), rec("good", network="mainnet", wallet=VALID))
    _write(
        str(tmp_path / "fixture.json"),
        rec("fixture", network="testnet", wallet="rUSERUSERUSERUSERUSERUSERUSERUSER"),
    )

    resumed = burn2mint_flow.load_all_resumable()
    assert [s.id for s in resumed] == ["good"]
    with open(tmp_path / "fixture.json") as f:
        rec_ = json.load(f)
    assert rec_["state"] == burn2mint_flow.FAILED
    assert "refused at resume" in rec_["error"]
    assert [s.id for s in burn2mint_flow.load_all_resumable()] == ["good"]
