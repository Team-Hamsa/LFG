# Tests for lfg_core/history_events.py
import os
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

from lfg_core import history_events
from tests.fixtures import history_txs as fx


def _nft(tx):
    return history_events.derive_nft_events(tx, nft_issuer=fx.ISSUER)


def test_mint():
    (ev,) = _nft(fx.MINT)
    assert ev["event"] == "mint" and ev["nft_id"] == fx.NFT_A
    assert ev["to_addr"] == fx.ISSUER
    assert ev["ts"] == 800000000 + history_events.RIPPLE_EPOCH


def test_burn_records_owner():
    (ev,) = _nft(fx.BURN)
    assert ev["event"] == "burn" and ev["from_addr"] == fx.ALICE


def test_modify_is_swap():
    (ev,) = _nft(fx.MODIFY)
    assert ev["event"] == "modify" and ev["to_addr"] == fx.ALICE


def test_sale_xrp_seller_buyer_price():
    (ev,) = _nft(fx.SALE_XRP)
    assert ev["event"] == "sale"
    assert (ev["from_addr"], ev["to_addr"]) == (fx.ALICE, fx.BOB)
    assert ev["price_drops"] == 5000000 and ev["price_token"] is None


def test_zero_price_is_transfer():
    (ev,) = _nft(fx.TRANSFER_FREE)
    assert ev["event"] == "transfer"
    assert (ev["from_addr"], ev["to_addr"]) == (fx.ISSUER, fx.ALICE)


def test_buy_offer_iou_sale():
    (ev,) = _nft(fx.SALE_IOU)
    assert ev["event"] == "sale"
    assert (ev["from_addr"], ev["to_addr"]) == (fx.ALICE, fx.BOB)
    assert ev["price_drops"] is None and '"value": "10"' in ev["price_token"]


def test_accept_offer_uses_authoritative_offer_nftoken_id():
    """The deleted offer's own NFTokenID must win over affected_nft_ids'
    page-diff fallback, which can surface an unrelated token shuffled between
    NFTokenPages in the same tx."""
    (ev,) = _nft(fx.SALE_XRP_PAGE_DIFF_MISMATCH)
    assert ev["nft_id"] == fx.NFT_A
    assert ev["nft_id"] != fx.NFT_B


def test_brokered_sale_attribution():
    (ev,) = _nft(fx.SALE_BROKERED)
    assert ev["event"] == "sale"
    assert (ev["from_addr"], ev["to_addr"]) == (fx.ALICE, fx.BOB)
    assert ev["price_drops"] == 6000000 and ev["price_token"] is None


def test_zero_value_iou_is_transfer():
    (ev,) = _nft(fx.TRANSFER_ZERO_IOU)
    assert ev["event"] == "transfer"
    assert ev["price_drops"] is None and ev["price_token"] is None


def test_missing_amount_is_transfer():
    (ev,) = _nft(fx.TRANSFER_NO_AMOUNT)
    assert ev["event"] == "transfer"
    assert ev["price_drops"] is None and ev["price_token"] is None


def test_offer_create_and_cancel():
    (c,) = _nft(fx.OFFER_CREATE)
    assert c["event"] == "offer_create" and c["price_drops"] == 9000000
    (x,) = _nft(fx.OFFER_CANCEL)
    assert x["event"] == "offer_cancel" and x["nft_id"] == fx.NFT_A


def test_non_nft_tx_yields_nothing():
    assert _nft(fx.AIRDROP) == []


def test_tec_burn_derives_no_events():
    """#235: a tec-class NFTokenBurn is ledger-included but burned nothing —
    it must not become a burn event (5 'burns' were once recorded for one
    token from failed attempts)."""
    assert _nft(fx.BURN_TEC) == []


def test_tec_tx_derives_no_events_for_any_type():
    """The strict tesSUCCESS gate covers every event type, not just burn."""
    for tx in (
        fx.MINT,
        fx.BURN,
        fx.MODIFY,
        fx.SALE_XRP,
        fx.SALE_BROKERED,
        fx.OFFER_CREATE,
        fx.OFFER_CANCEL,
    ):
        assert _nft(tx), "fixture must derive when tesSUCCESS"
        assert _nft(fx.failed(tx)) == []
        assert _nft(fx.failed(tx, "tecINSUFFICIENT_RESERVE")) == []


def test_missing_result_derives_no_events():
    """Strict gate: a record with no TransactionResult (or no meta at all) is
    not provably successful — skipped. Real ledger records always carry it."""
    no_result = dict(fx.BURN)
    no_result["meta"] = {"AffectedNodes": []}
    assert _nft(no_result) == []
    no_meta = {k: v for k, v in fx.BURN.items() if k != "meta"}
    assert _nft(no_meta) == []


def test_normalize_entry_account_tx_shape():
    entry = {
        "tx": {"TransactionType": "Payment", "Account": "rX", "date": 1},
        "meta": {"AffectedNodes": []},
        "hash": "FF" * 32,
        "ledger_index": 42,
        "validated": True,
    }
    tx = history_events.normalize_entry(entry)
    assert tx["hash"] == "FF" * 32 and tx["ledger_index"] == 42
    assert tx["meta"] == {"AffectedNodes": []}


def _brix(tx, distributor=None):
    return history_events.derive_brix_events(
        tx, brix_issuer=fx.BRIX_ISSUER, brix_hex=fx.BRIX_HEX, distributor=distributor
    )


def test_airdrop_deltas_and_kind():
    evs = _brix(fx.AIRDROP, distributor=fx.DISTRIBUTOR)
    by = {e["account"]: e for e in evs}
    assert by[fx.ALICE]["delta"] == 3.0 and by[fx.ALICE]["kind"] == "airdrop"
    assert by[fx.DISTRIBUTOR]["delta"] == -3.0
    assert by[fx.ALICE]["counterparty"] == fx.DISTRIBUTOR


def test_payment_without_distributor_is_payment():
    evs = _brix(fx.AIRDROP)
    assert all(e["kind"] == "payment" for e in evs)


def test_trustset_kind():
    evs = _brix(fx.TRUSTSET)
    assert evs == [] or all(e["kind"] == "trustset" for e in evs)


def test_amm_deposit_kind():
    evs = _brix(fx.AMM_DEPOSIT)
    assert len(evs) == 1 and evs[0]["kind"] == "amm_deposit"
    assert evs[0]["delta"] == -10.0


def test_non_brix_tx_no_events():
    assert _brix(fx.SALE_XRP) == []


def test_tec_payment_derives_no_brix_events():
    """A failed Payment moved no balances — no BRIX events, even if the fixture
    meta still carries RippleState diffs (the result code is the gate)."""
    assert _brix(fx.failed(fx.AIRDROP), distributor=fx.DISTRIBUTOR) == []
    assert _brix(fx.failed(fx.AMM_DEPOSIT)) == []


def test_issuer_account_hex_roundtrip():
    hexid = history_events.issuer_account_hex(fx.ISSUER)
    assert len(hexid) == 40 and hexid == hexid.upper()


def test_nft_id_issuer_matches():
    ihex = history_events.issuer_account_hex(fx.ISSUER)
    assert history_events.nft_id_issuer_matches(fx.NFT_A, ihex)
    assert not history_events.nft_id_issuer_matches(fx.NFT_FOREIGN, ihex)
    # malformed ids never match
    assert not history_events.nft_id_issuer_matches("", ihex)
    assert not history_events.nft_id_issuer_matches("000A", ihex)
    assert not history_events.nft_id_issuer_matches(None, ihex)  # type: ignore[arg-type]


def test_brix_deltas_skips_malformed_ripplestate():
    # RippleState node with a non-string holder issuer must be skipped, not crash.
    meta = {
        "TransactionResult": "tesSUCCESS",
        "AffectedNodes": [
            {
                "ModifiedNode": {
                    "LedgerEntryType": "RippleState",
                    "FinalFields": {
                        "Balance": {"currency": fx.BRIX_HEX, "value": "5"},
                        "HighLimit": {"issuer": fx.BRIX_ISSUER, "currency": fx.BRIX_HEX},
                        "LowLimit": {"currency": fx.BRIX_HEX},  # missing issuer -> holder None
                    },
                    "PreviousFields": {"Balance": {"currency": fx.BRIX_HEX, "value": "2"}},
                }
            }
        ],
    }
    tx = {"TransactionType": "Payment", "Account": fx.ALICE, "hash": "AB" * 32, "meta": meta}
    assert (
        history_events.derive_brix_events(tx, brix_issuer=fx.BRIX_ISSUER, brix_hex=fx.BRIX_HEX)
        == []
    )


def test_mint_memo_action_extracted():
    # Provenance memos (#54): the deriver must surface the `action` memo so
    # the leaderboard can tell an economy assemble-remint from a legacy
    # burn+remint trait swap (mainnet has 0 modify events — every legacy swap
    # is a rebirth, and without this they all masquerade as "builds").
    from xrpl.utils import str_to_hex

    tx = dict(fx.MINT)
    tx["Memos"] = [
        {"Memo": {"MemoType": str_to_hex("initiator"), "MemoData": str_to_hex("backend")}},
        {"Memo": {"MemoType": str_to_hex("action"), "MemoData": str_to_hex("assemble")}},
    ]
    (ev,) = _nft(tx)
    assert ev["memo_action"] == "assemble"


def test_mint_without_memos_has_no_action():
    (ev,) = _nft(fx.MINT)
    assert ev["memo_action"] is None


def test_action_memo_with_absent_data_is_none_not_empty_string():
    # MemoType says "action" but MemoData is missing: the contract is None,
    # not "" (Greptile #157) — an empty string would silently occupy the
    # memo_action column and read as a distinct "action" downstream.
    from xrpl.utils import str_to_hex

    tx = dict(fx.MINT)
    tx["Memos"] = [{"Memo": {"MemoType": str_to_hex("action")}}]
    (ev,) = _nft(tx)
    assert ev["memo_action"] is None


def test_malformed_memos_do_not_break_derivation():
    tx = dict(fx.MINT)
    tx["Memos"] = [
        "garbage",
        {"Memo": {"MemoType": "ZZNOTHEX", "MemoData": "ALSONOTHEX"}},
        {"Memo": {}},
    ]
    (ev,) = _nft(tx)
    assert ev["event"] == "mint" and ev["memo_action"] is None
