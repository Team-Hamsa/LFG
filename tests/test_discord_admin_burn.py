# The Discord /admin "Confirm Burn" button: only a confirmed burn mutates the
# LFG / burned_nfts tables; an unknown on-ledger outcome is reported as such,
# never as a clean failure and never recorded as a burn.
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

_ENV = {
    "DISCORD_BOT_TOKEN": "t",
    "ADMIN_LOG_CHANNEL_ID": "1",
    "LFG_SERVICE_URL": "http://svc",
    "SERVICE_TOKEN_DISCORD": "s",
}


class _Followup:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content or "")


class _Response:
    async def defer(self, **kwargs):
        return None


def _interaction():
    return SimpleNamespace(
        response=_Response(),
        followup=_Followup(),
        user=SimpleNamespace(id=42, mention="<@42>"),
        guild=None,
    )


@pytest.fixture
def admin(monkeypatch, tmp_path):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    # NB: plain import, never reload — reloading re-registers @tree.command.
    import surfaces.discord_bot.admin as admin

    db = tmp_path / "lfg.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE LFG (nft_number INTEGER PRIMARY KEY, nft_id TEXT, "
        "discord_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE burned_nfts (nft_number INTEGER PRIMARY KEY, nft_id TEXT, "
        "discord_id TEXT, burned_by TEXT, reason TEXT, "
        "burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, original_mint_time TIMESTAMP)"
    )
    conn.execute("INSERT INTO LFG (nft_number, nft_id, discord_id) VALUES (7, 'NFT7', 'u1')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(admin.core_config, "DB_PATH", str(db))
    monkeypatch.setattr(admin._rarity, "connect", lambda: sqlite3.connect(":memory:"))
    monkeypatch.setattr(admin._rarity, "recalculate_rarity", lambda conn: None)
    return admin, db


def _press_confirm(monkeypatch, admin, outcome):
    async def fake_burn(nft_id):
        assert nft_id == "NFT7"
        return outcome

    monkeypatch.setattr(admin, "burn_nft", fake_burn)

    async def go():
        view = admin.BurnConfirmView(7, "NFT7", "test reason")
        interaction = _interaction()
        await view.confirm_burn.callback(interaction)
        return interaction

    return asyncio.run(go())


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        lfg = conn.execute("SELECT nft_number FROM LFG").fetchall()
        burned = conn.execute("SELECT nft_number, burned_by, reason FROM burned_nfts").fetchall()
        return lfg, burned
    finally:
        conn.close()


def test_confirmed_burn_records_audit_row(monkeypatch, admin):
    mod, db = admin
    interaction = _press_confirm(monkeypatch, mod, mod.BurnOutcome(mod.BURN_BURNED, "H" * 64))
    assert _rows(db) == ([], [(7, "42", "test reason")])
    assert "Successfully burned NFT #7" in interaction.followup.sent[-1]


def test_indeterminate_burn_leaves_records_and_says_unknown(monkeypatch, admin):
    mod, db = admin
    interaction = _press_confirm(
        monkeypatch, mod, mod.BurnOutcome(mod.BURN_INDETERMINATE, "C" * 64)
    )
    assert _rows(db) == ([(7,)], [])
    msg = interaction.followup.sent[-1]
    assert "outcome is unknown" in msg and "C" * 64 in msg
    assert "Failed" not in msg


def test_failed_burn_leaves_records(monkeypatch, admin):
    mod, db = admin
    interaction = _press_confirm(monkeypatch, mod, mod.BurnOutcome(mod.BURN_FAILED))
    assert _rows(db) == ([(7,)], [])
    assert "Failed to burn NFT #7" in interaction.followup.sent[-1]
