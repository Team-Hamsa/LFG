# tests/test_brix_pure_js.py
# Issue #48 PR-3 (Activity BRIX card). The card's render + error decisions are
# pure functions in webapp/client/brix_pure.js, executed here under Node —
# same harness as tests/test_mint_pure_js.py / tests/test_market_pure_js.py.
#
# No lfg_core import at module top -> no env-guard preamble needed.
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/brix_pure.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed on this host")


def run_js(expr: str):
    """Run `expr` (a JS expression referencing the imported module as `M`)
    inside a small Node ES-module script, executed with cwd=ROOT so the
    relative import resolves; returns the JSON-decoded result."""
    script = (
        f"import * as M from {json.dumps(MODULE_REL)};\n"
        f"const result = ({expr});\n"
        f"console.log(JSON.stringify(result === undefined ? null : result));\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=15,
    )
    assert proc.returncode == 0, f"node script failed:\n{script}\n--- stderr ---\n{proc.stderr}"
    return json.loads(proc.stdout)


def view(status):
    return run_js(f"M.brixCardView({json.dumps(status)})")


BASE = {
    "wallet": "rUSER",
    "claimable": 0,
    "unlisted_last_epoch": 0,
    "accrued_total": 0,
    "claimed_total": 0,
    "open_claim": None,
    "last_epoch": None,
}


def with_(**kw):
    s = dict(BASE)
    s.update(kw)
    return s


# ---------------------------------------------------------------------------
# brixCardView(status) — what the home-screen card renders
# ---------------------------------------------------------------------------


def test_hidden_when_the_drip_has_never_run():
    """Accrual unarmed (no epoch recorded, nothing ever accrued): the card
    must not advertise a feature that cannot pay out yet."""
    assert view(BASE)["visible"] is False


def test_visible_once_an_epoch_has_been_accrued():
    v = view(with_(last_epoch="2026-08-19"))
    assert v["visible"] is True


def test_visible_from_history_alone_even_if_meta_is_missing():
    """A wallet with past accruals must still see its balance if the meta row
    is unreadable — hiding the card would hide real money."""
    assert view(with_(accrued_total=9, claimed_total=9))["visible"] is True


def test_claimable_balance_drives_the_button():
    v = view(with_(last_epoch="2026-08-19", claimable=12, unlisted_last_epoch=12))
    assert v["claimable"] == 12
    assert v["headline"] == "12 BRIX"
    assert v["button"]["disabled"] is False
    assert "12" in v["button"]["label"]
    assert v["pollClaimId"] is None


def test_singular_unit_is_not_pluralised():
    v = view(with_(last_epoch="2026-08-19", claimable=1))
    assert v["headline"] == "1 BRIX"


def test_zero_claimable_disables_the_button():
    v = view(with_(last_epoch="2026-08-19", claimed_total=5))
    assert v["visible"] is True
    assert v["claimable"] == 0
    assert v["button"]["disabled"] is True


def test_earning_count_is_surfaced():
    v = view(with_(last_epoch="2026-08-19", claimable=3, unlisted_last_epoch=3))
    assert "3" in v["sub"]


def test_listed_nfts_earn_nothing_is_explained_when_nothing_earned():
    """unlisted_last_epoch == 0 with an accrued epoch means every NFT was
    listed (or none owned) — say so rather than showing a bare zero."""
    v = view(with_(last_epoch="2026-08-19"))
    assert v["sub"]


def test_open_claim_puts_the_card_in_flight():
    v = view(
        with_(
            last_epoch="2026-08-19",
            claimable=0,
            open_claim={"claim_id": 7, "state": "pending", "tx_hash": None},
        )
    )
    assert v["pollClaimId"] == 7
    assert v["button"]["disabled"] is True
    assert v["inFlight"] is True


def test_open_claim_disables_the_button_even_with_a_balance():
    """A second claim would be refused (claim_in_flight) — never offer it."""
    v = view(
        with_(
            last_epoch="2026-08-19",
            claimable=4,
            open_claim={"claim_id": 8, "state": "submitted", "tx_hash": "ABC"},
        )
    )
    assert v["button"]["disabled"] is True
    assert v["pollClaimId"] == 8


def test_null_status_is_invisible_not_a_crash():
    assert view(None)["visible"] is False


# ---------------------------------------------------------------------------
# claimErrorView(code) — how each documented failure code is handled
# ---------------------------------------------------------------------------


def err(code):
    return run_js(f"M.claimErrorView({json.dumps(code)})")


def test_unconfirmed_claim_is_never_retryable():
    """The single most important rule: the server already bound the accruals,
    so a client retry would double-claim. #407 / SDK precedent."""
    v = err("claim_unconfirmed")
    assert v["retryable"] is False
    assert v["refresh"] is True


def test_claims_disabled_is_not_retryable_and_says_accrual_continues():
    v = err("claims_disabled")
    assert v["retryable"] is False
    assert "accru" in v["message"].lower()


def test_trustline_required_points_at_the_trustline():
    v = err("trustline_required")
    assert v["retryable"] is False
    assert "trustline" in v["message"].lower()
    assert v["trustline"] is True


def test_claim_unavailable_is_retryable():
    """Nothing was bound server-side, so retrying is safe and correct."""
    v = err("claim_unavailable")
    assert v["retryable"] is True


def test_in_flight_and_nothing_to_claim_just_refresh():
    for code in ("claim_in_flight", "nothing_to_claim"):
        v = err(code)
        assert v["refresh"] is True, code
        assert v["retryable"] is False, code


def test_unknown_code_has_a_generic_message():
    v = err("something_new")
    assert v["message"]
    assert v["retryable"] is False


# ---------------------------------------------------------------------------
# isClaimTerminal(state) — when the status poll stops
# ---------------------------------------------------------------------------


def test_terminal_states():
    assert run_js("M.isClaimTerminal('confirmed')") is True
    assert run_js("M.isClaimTerminal('failed')") is True


def test_non_terminal_states_keep_polling():
    for state in ("pending", "submitted", "unknown"):
        assert run_js(f"M.isClaimTerminal({json.dumps(state)})") is False


def test_unknown_state_is_not_treated_as_terminal():
    assert run_js("M.isClaimTerminal(null)") is False
