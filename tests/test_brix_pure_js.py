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


def test_hidden_views_still_answer_the_claim_fields():
    """claimBrix() re-reads /api/brix and gates on `view.claimable <= 0 ||
    view.inFlight` without a visibility check; a hidden view must therefore
    carry the full shape (claimable 0, not in flight) so an empty or unarmed
    refresh can never fall through to the confirm dialog."""
    for v in (view(None), view(BASE)):
        assert v["visible"] is False
        assert v["claimable"] == 0
        assert v["inFlight"] is False
        assert v["pollClaimId"] is None
        assert v["button"]["disabled"] is True


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
    # #441: the lock is actionable — the pinned label invites the TrustSet
    # flow instead of telling the user to go do it by hand in Xaman.
    assert v["lockLabel"] == "Set BRIX trustline"
    assert "xaman" not in v["message"].lower()
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


def test_server_side_preconditions_carry_a_button_lock_label():
    """claims_disabled / trustline_required must keep the button OFF after the
    card reloads — the refreshed status still shows a positive balance (the
    server cannot express either condition in GET /api/brix), which would
    otherwise re-enable "Claim N BRIX" right under a toast saying don't retry."""
    for code in ("claims_disabled", "trustline_required"):
        v = err(code)
        assert v["retryable"] is False, code
        assert v["refresh"] is False, code
        assert isinstance(v["lockLabel"], str) and v["lockLabel"], code


def test_refresh_codes_never_lock():
    """Codes whose truth is in the refreshed status must NOT pin the button:
    claim_in_flight / nothing_to_claim / claim_unconfirmed are all resolved by
    the next GET /api/brix (an open claim disables + polls; a balance that
    comes back after a concurrent claim fails must be claimable again without
    leaving the home screen). Greptile P1 #2 on PR #434."""
    for code in (
        "claim_in_flight",
        "nothing_to_claim",
        "claim_unconfirmed",
        "something_new",
        "claim_unavailable",
    ):
        v = err(code)
        assert v["lockLabel"] is None, code
        assert v["refresh"] is True, code


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


# --- BRIX trustline flow view (#441) -------------------------------------------


def tl(state):
    return run_js(f"M.trustlineView({json.dumps(state)})")


def test_trustline_view_pending_and_opened_keep_waiting():
    for state in ("pending", "opened"):
        v = tl(state)
        assert v["terminal"] is False, state
        assert v["retry"] is False, state
    assert tl("opened")["spinner"] is True


def test_trustline_view_signed_is_terminal_and_clears_the_lock():
    v = tl("signed")
    assert v["terminal"] is True
    assert v["clearLock"] is True
    assert v["retry"] is False


def test_trustline_view_already_set_behaves_like_signed():
    v = tl("already_set")
    assert v["terminal"] is True
    assert v["clearLock"] is True


def test_trustline_view_failures_are_terminal_and_retryable_but_keep_the_lock():
    for state in ("expired", "rejected"):
        v = tl(state)
        assert v["terminal"] is True, state
        assert v["retry"] is True, state
        assert v["clearLock"] is False, state


def test_trustline_view_unknown_state_keeps_polling():
    v = tl("something_new")
    assert v["terminal"] is False


def test_trustline_terminal_helper():
    assert run_js("M.isTrustlineTerminal('signed')") is True
    assert run_js("M.isTrustlineTerminal('pending')") is False


def test_trustline_view_validating_keeps_polling_with_spinner():
    v = tl("validating")
    assert v["spinner"] is True and v["terminal"] is False and v["clearLock"] is False


def test_trustline_view_rejected_tx_failed_is_distinct_from_signer_mismatch():
    failed = run_js('M.trustlineView("rejected", "tx_failed")')
    mismatch = run_js('M.trustlineView("rejected", "signer_mismatch")')
    assert failed["retry"] and failed["terminal"] and not failed["clearLock"]
    assert failed["sub"] != mismatch["sub"]
    assert "ledger" in failed["sub"]


# --- claim-all across linked wallets (#446) --------------------------------


def summary(status):
    return run_js(f"M.linkedClaimSummary({json.dumps(status)})")


def test_summary_no_linked_field_is_single_claim():
    s = summary(with_(claimable=3))
    assert s["multi"] is False
    assert s["useClaimAll"] is False
    assert s["total"] == 0


def test_summary_two_positive_wallets_is_claim_all():
    s = summary(
        with_(
            claimable=3,
            linked=[
                {"wallet": "rUSER", "claimable": 3},
                {"wallet": "rB", "claimable": 2},
                {"wallet": "rC", "claimable": 0},
            ],
        )
    )
    assert s["multi"] is True
    assert s["useClaimAll"] is True
    assert s["total"] == 5
    assert [w["wallet"] for w in s["wallets"]] == ["rUSER", "rB"]


def test_summary_balance_only_on_a_linked_wallet_uses_claim_all():
    """A solo POST /api/brix/claim would 400 nothing_to_claim here."""
    s = summary(
        with_(
            claimable=0,
            linked=[{"wallet": "rUSER", "claimable": 0}, {"wallet": "rB", "claimable": 2}],
        )
    )
    assert s["multi"] is False
    assert s["useClaimAll"] is True
    assert s["total"] == 2


def test_summary_own_wallet_only_stays_single_claim():
    s = summary(
        with_(
            claimable=3,
            linked=[{"wallet": "rUSER", "claimable": 3}, {"wallet": "rB", "claimable": 0}],
        )
    )
    assert s["useClaimAll"] is False


def test_card_shows_the_combined_linked_balance():
    v = view(
        with_(
            claimable=1,
            last_epoch="2026-08-18",
            linked=[{"wallet": "rUSER", "claimable": 1}, {"wallet": "rB", "claimable": 4}],
        )
    )
    assert v["claimable"] == 5
    assert v["button"]["label"] == "Claim 5 BRIX"


def test_card_is_visible_on_a_linked_balance_even_with_no_own_history():
    v = view(with_(linked=[{"wallet": "rUSER", "claimable": 0}, {"wallet": "rB", "claimable": 2}]))
    assert v["visible"] is True
    assert v["claimable"] == 2


def test_card_without_linked_balances_is_unchanged():
    assert view(with_())["visible"] is False


def row(r):
    return run_js(f"M.claimAllRowView({json.dumps(r)})")


def test_row_trustline_required_offers_the_action():
    r = row({"wallet": "rB", "status": "trustline_required", "claimable": 2})
    assert r["trustline"] is True
    assert r["ok"] is False


def test_row_confirmed_shows_the_paid_amount():
    r = row({"wallet": "rB", "status": "confirmed", "amount": 4})
    assert r["ok"] is True
    assert "4 BRIX" in r["text"]


def test_row_unknown_status_keeps_working_never_a_verdict():
    r = row({"wallet": "rB", "status": "someday_new_state"})
    assert r["spinner"] is True
    assert r["ok"] is False
    assert r["trustline"] is False


def job(j):
    return run_js(f"M.claimAllJobView({json.dumps(j)})")


def test_job_running_is_not_terminal():
    v = job({"state": "running", "wallets": [{"wallet": "rA", "status": "claiming"}]})
    assert v["terminal"] is False


def test_job_done_counts_paid_and_flags_missing_trustlines():
    v = job(
        {
            "state": "done",
            "wallets": [
                {"wallet": "rA", "status": "confirmed", "amount": 3},
                {"wallet": "rB", "status": "trustline_required", "claimable": 2},
            ],
        }
    )
    assert v["terminal"] is True
    assert "1 of 2" in v["sub"]
    assert "trustline" in v["sub"]
