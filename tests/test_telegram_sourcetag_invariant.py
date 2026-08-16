# tests/test_telegram_sourcetag_invariant.py
# The Telegram surface builds NO inline XRPL/XUMM transactions — all minting
# goes through lfg_service, which stamps the Make Waves SourceTag (covered by
# test_xrpl_source_tag.py + test_xumm_source_tag.py). This test pins that
# invariant: no TransactionType / source_tag / NFToken construction appears in
# the Telegram package source.
import pathlib

_PKG = pathlib.Path(__file__).resolve().parent.parent / "surfaces" / "telegram_bot"

_FORBIDDEN = ("TransactionType", "NFTokenMint", "NFTokenBurn", "TrustSet", "submit_and_wait")


def test_no_inline_xrpl_tx_in_telegram_package():
    offenders = []
    # rglob (not glob) so the invariant still catches an inline tx if a future
    # subpackage is added (e.g. surfaces/telegram_bot/handlers/foo.py).
    for py in _PKG.rglob("*.py"):
        text = py.read_text()
        for needle in _FORBIDDEN:
            if needle in text:
                offenders.append((py.name, needle))
    assert offenders == [], f"unexpected inline tx tokens in telegram package: {offenders}"
