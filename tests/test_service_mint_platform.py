from lfg_core.mint_flow import MintSession


def test_mint_session_defaults_platform_discord():
    s = MintSession(discord_id="9", wallet_address="rA")
    assert s.platform == "discord"
    assert s.to_dict()["platform"] == "discord"


def test_mint_session_accepts_platform():
    s = MintSession(discord_id="55", wallet_address="rB", platform="telegram")
    assert s.platform == "telegram"
    assert s.to_dict()["platform"] == "telegram"
