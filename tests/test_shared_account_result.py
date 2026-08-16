# tests/test_shared_account_result.py
from surfaces._shared.account_result import linked_summary, other_surfaces


def _acct(identities, wallet="rW"):
    return {"wallet": wallet, "identities": identities}


def test_other_surfaces_excludes_current():
    acct = _acct(
        [
            {"platform": "discord", "platform_user_id": "D", "display_handle": "alice"},
            {"platform": "telegram", "platform_user_id": "T", "display_handle": "alice_tg"},
        ]
    )
    out = other_surfaces(acct, current_platform="telegram", current_user_id="T")
    assert out == [("discord", "alice")]


def test_other_surfaces_empty_when_only_self():
    acct = _acct([{"platform": "telegram", "platform_user_id": "T", "display_handle": "tg"}])
    assert other_surfaces(acct, current_platform="telegram", current_user_id="T") == []


def test_linked_summary_lists_other_surfaces():
    acct = _acct(
        [
            {"platform": "discord", "platform_user_id": "D", "display_handle": "alice"},
            {"platform": "telegram", "platform_user_id": "T", "display_handle": "alice_tg"},
        ]
    )
    msg = linked_summary(acct, current_platform="telegram", current_user_id="T")
    assert "Discord" in msg
    assert "alice" in msg


def test_linked_summary_no_other_surfaces():
    acct = _acct([{"platform": "telegram", "platform_user_id": "T", "display_handle": "tg"}])
    msg = linked_summary(acct, current_platform="telegram", current_user_id="T")
    assert msg  # non-empty, references no other surface
    assert "Discord" not in msg
