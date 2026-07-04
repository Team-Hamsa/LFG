"""Canned normalized XRPL tx dicts (tx fields top-level + `meta`) for
derivation tests. Shapes mirror clio account_tx / nft_history output after
history_events.normalize_entry."""

from xrpl.core import addresscodec


def _addr(tag: int) -> str:
    """Deterministic VALID classic address (decodable by addresscodec)."""
    return addresscodec.encode_classic_address(bytes([tag]) * 20)


def nft_id_for(issuer: str, *, flags: int = 10, fee: int = 0, taxon: int = 0, seq: int = 1) -> str:
    """Build a valid 64-hex NFTokenID embedding `issuer`'s AccountID at
    bytes 4-24 (hex chars 8..48), like the ledger does."""
    acct = addresscodec.decode_classic_address(issuer).hex().upper()
    return f"{flags:04X}{fee:04X}{acct}{taxon:08X}{seq:08X}"


ISSUER = _addr(0x01)
BRIX_ISSUER = _addr(0x02)
DISTRIBUTOR = _addr(0x03)
ALICE = _addr(0x04)
BOB = _addr(0x05)
BROKER = _addr(0x06)
FOREIGN_ISSUER = _addr(0x07)
NFT_A = nft_id_for(ISSUER)
NFT_FOREIGN = nft_id_for(FOREIGN_ISSUER)
BRIX_HEX = "4252495800000000000000000000000000000000"

MINT = {
    "TransactionType": "NFTokenMint",
    "Account": ISSUER,
    "hash": "01" * 32,
    "ledger_index": 100,
    "date": 800000000,
    "meta": {"nftoken_id": NFT_A, "AffectedNodes": []},
}

BURN = {
    "TransactionType": "NFTokenBurn",
    "Account": ISSUER,
    "Owner": ALICE,
    "NFTokenID": NFT_A,
    "hash": "02" * 32,
    "ledger_index": 101,
    "date": 800000100,
    "meta": {"AffectedNodes": []},
}

# Foreign-collection txs (nft_id embeds FOREIGN_ISSUER): the listener firehose
# sees every network tx, so these must be filtered OUT of the history archive.
FOREIGN_BURN = {
    "TransactionType": "NFTokenBurn",
    "Account": FOREIGN_ISSUER,
    "Owner": BOB,
    "NFTokenID": NFT_FOREIGN,
    "hash": "F1" * 32,
    "ledger_index": 201,
    "date": 800009100,
    "meta": {"AffectedNodes": []},
}

FOREIGN_MODIFY = {
    "TransactionType": "NFTokenModify",
    "Account": FOREIGN_ISSUER,
    "Owner": BOB,
    "NFTokenID": NFT_FOREIGN,
    "hash": "F2" * 32,
    "ledger_index": 202,
    "date": 800009200,
    "meta": {"AffectedNodes": []},
}

MODIFY = {
    "TransactionType": "NFTokenModify",
    "Account": ISSUER,
    "Owner": ALICE,
    "NFTokenID": NFT_A,
    "hash": "03" * 32,
    "ledger_index": 102,
    "date": 800000200,
    "meta": {"AffectedNodes": []},
}


def _deleted_offer(owner, amount, flags):
    return {
        "DeletedNode": {
            "LedgerEntryType": "NFTokenOffer",
            "FinalFields": {"Owner": owner, "Amount": amount, "Flags": flags, "NFTokenID": NFT_A},
        }
    }


# Alice sells to Bob for 5 XRP (Bob accepts Alice's sell offer, flag 1)
SALE_XRP = {
    "TransactionType": "NFTokenAcceptOffer",
    "Account": BOB,
    "hash": "04" * 32,
    "ledger_index": 103,
    "date": 800000300,
    "meta": {"nftoken_id": NFT_A, "AffectedNodes": [_deleted_offer(ALICE, "5000000", 1)]},
}

# Issuer transfers to Alice for 0 (zero-price sell offer)
TRANSFER_FREE = {
    "TransactionType": "NFTokenAcceptOffer",
    "Account": ALICE,
    "hash": "05" * 32,
    "ledger_index": 104,
    "date": 800000400,
    "meta": {"nftoken_id": NFT_A, "AffectedNodes": [_deleted_offer(ISSUER, "0", 1)]},
}

# Bob buys from Alice with a BUY offer (flag 0): offer.Owner = buyer, accepter = seller
SALE_IOU = {
    "TransactionType": "NFTokenAcceptOffer",
    "Account": ALICE,
    "hash": "06" * 32,
    "ledger_index": 105,
    "date": 800000500,
    "meta": {
        "nftoken_id": NFT_A,
        "AffectedNodes": [
            _deleted_offer(BOB, {"currency": BRIX_HEX, "issuer": BRIX_ISSUER, "value": "10"}, 0)
        ],
    },
}

# Brokered sale: broker (third party) accepts BOTH a sell offer (Alice, flag 1)
# and a buy offer (Bob, flag 0) in the same tx. tx.Account is the broker, not
# the buyer. Seller = sell offer Owner (Alice), buyer = buy offer Owner (Bob),
# price = the BUY offer's Amount (what the buyer paid), not the sell offer's ask.
SALE_BROKERED = {
    "TransactionType": "NFTokenAcceptOffer",
    "Account": BROKER,
    "hash": "0C" * 32,
    "ledger_index": 111,
    "date": 800001100,
    "meta": {
        "nftoken_id": NFT_A,
        "AffectedNodes": [
            _deleted_offer(ALICE, "5000000", 1),
            _deleted_offer(BOB, "6000000", 0),
        ],
    },
}

# Zero-value IOU offer accepted: must classify as transfer, not sale.
TRANSFER_ZERO_IOU = {
    "TransactionType": "NFTokenAcceptOffer",
    "Account": BOB,
    "hash": "0D" * 32,
    "ledger_index": 112,
    "date": 800001200,
    "meta": {
        "nftoken_id": NFT_A,
        "AffectedNodes": [
            _deleted_offer(ALICE, {"currency": BRIX_HEX, "issuer": BRIX_ISSUER, "value": "0"}, 1)
        ],
    },
}


def _deleted_offer_no_amount(owner, flags):
    return {
        "DeletedNode": {
            "LedgerEntryType": "NFTokenOffer",
            "FinalFields": {"Owner": owner, "Flags": flags, "NFTokenID": NFT_A},
        }
    }


def _nftoken_page_node(nft_id):
    return {
        "ModifiedNode": {
            "LedgerEntryType": "NFTokenPage",
            "FinalFields": {"NFTokens": [{"NFToken": {"NFTokenID": nft_id}}]},
        }
    }


NFT_B = nft_id_for(ISSUER, seq=2)

# No meta.nftoken_id (clio omits it on some entries) and a NFTokenPage diff
# that would surface NFT_B via affected_nft_ids' page-diff fallback — but the
# deleted offer's own NFTokenID (authoritative) says NFT_A actually traded.
SALE_XRP_PAGE_DIFF_MISMATCH = {
    "TransactionType": "NFTokenAcceptOffer",
    "Account": BOB,
    "hash": "0F" * 32,
    "ledger_index": 114,
    "date": 800001400,
    "meta": {
        "AffectedNodes": [
            _deleted_offer(ALICE, "5000000", 1),
            _nftoken_page_node(NFT_B),
        ]
    },
}

# Deleted offer with no Amount key at all: must classify as transfer.
TRANSFER_NO_AMOUNT = {
    "TransactionType": "NFTokenAcceptOffer",
    "Account": BOB,
    "hash": "0E" * 32,
    "ledger_index": 113,
    "date": 800001300,
    "meta": {
        "nftoken_id": NFT_A,
        "AffectedNodes": [_deleted_offer_no_amount(ALICE, 1)],
    },
}

OFFER_CREATE = {
    "TransactionType": "NFTokenCreateOffer",
    "Account": ALICE,
    "NFTokenID": NFT_A,
    "Amount": "9000000",
    "Flags": 1,
    "hash": "07" * 32,
    "ledger_index": 106,
    "date": 800000600,
    "meta": {"AffectedNodes": []},
}

OFFER_CANCEL = {
    "TransactionType": "NFTokenCancelOffer",
    "Account": ALICE,
    "hash": "08" * 32,
    "ledger_index": 107,
    "date": 800000700,
    "meta": {"AffectedNodes": [_deleted_offer(ALICE, "9000000", 1)]},
}


def _ripplestate(holder, issuer, old, new, high_is_issuer=True):
    # holder as LOW account: Balance.value is the holder's (positive) balance
    low, high = (holder, issuer) if high_is_issuer else (issuer, holder)
    return {
        "ModifiedNode": {
            "LedgerEntryType": "RippleState",
            "FinalFields": {
                "Balance": {
                    "currency": BRIX_HEX,
                    "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji",
                    "value": str(new),
                },
                "HighLimit": {"issuer": high, "currency": BRIX_HEX, "value": "0"},
                "LowLimit": {"issuer": low, "currency": BRIX_HEX, "value": "0"},
            },
            "PreviousFields": {"Balance": {"currency": BRIX_HEX, "value": str(old)}},
        }
    }


# Distributor sends Alice 3 BRIX. Alice is LOW account (holder balance positive):
# old 10 -> new 13; distributor is low in its own line: old -50 ... keep one node
# per account for clarity.
AIRDROP = {
    "TransactionType": "Payment",
    "Account": DISTRIBUTOR,
    "Destination": ALICE,
    "Amount": {"currency": BRIX_HEX, "issuer": BRIX_ISSUER, "value": "3"},
    "hash": "09" * 32,
    "ledger_index": 108,
    "date": 800000800,
    "meta": {
        "AffectedNodes": [
            _ripplestate(ALICE, BRIX_ISSUER, 10, 13),
            _ripplestate(DISTRIBUTOR, BRIX_ISSUER, 50, 47),
        ]
    },
}

TRUSTSET = {
    "TransactionType": "TrustSet",
    "Account": BOB,
    "LimitAmount": {"currency": BRIX_HEX, "issuer": BRIX_ISSUER, "value": "1000000"},
    "hash": "0A" * 32,
    "ledger_index": 109,
    "date": 800000900,
    "meta": {"AffectedNodes": []},
}

AMM_DEPOSIT = {
    "TransactionType": "AMMDeposit",
    "Account": ALICE,
    "hash": "0B" * 32,
    "ledger_index": 110,
    "date": 800001000,
    "meta": {"AffectedNodes": [_ripplestate(ALICE, BRIX_ISSUER, 13, 3)]},
}
