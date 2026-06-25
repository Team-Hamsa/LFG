from surfaces._client.errors import BadRequest, ServiceError
from surfaces._shared.mint_result import BAD_STATE_MESSAGES, MINT_OK_STATES, friendly_error


def test_ok_states():
    assert "offer_ready" in MINT_OK_STATES and "done" in MINT_OK_STATES


def test_friendly_no_wallet():
    assert "register" in friendly_error(BadRequest("no wallet registered", status=400)).lower()


def test_friendly_in_progress():
    assert "in progress" in friendly_error(ServiceError("already", status=409)).lower()


def test_bad_state_messages_has_timeout():
    assert "timed out" in BAD_STATE_MESSAGES["payment_timeout"].lower()
