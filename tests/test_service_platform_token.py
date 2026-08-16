from lfg_service.app import make_session_token, verify_session_token


def test_token_roundtrips_explicit_platform():
    tok = make_session_token({"id": "55", "name": "tg", "platform": "telegram"})
    payload = verify_session_token(tok)
    assert payload["id"] == "55"
    assert payload["platform"] == "telegram"


def test_token_defaults_platform_to_discord():
    tok = make_session_token({"id": "9", "name": "d"})
    payload = verify_session_token(tok)
    assert payload["platform"] == "discord"
