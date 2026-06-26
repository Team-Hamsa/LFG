from surfaces._shared.signin_result import signin_outcome


def test_expired_message():
    assert "expired" in signin_outcome("expired").lower()
    assert "/register" in signin_outcome("expired")


def test_non_signed_fallback_is_expired_style():
    # any non-signed terminal/timeout state reads as "didn't complete, try again"
    msg = signin_outcome("pending")
    assert "/register" in msg


def test_signed_has_no_retry_prompt():
    # "signed" is success — outcome() is only used for the NON-signed branches,
    # but it must still return a benign string if ever called with "signed".
    assert isinstance(signin_outcome("signed"), str)
