def test_exchange_allowlist_covers_labelled_hot_wallets_but_not_farms():
    """2026-09-19: Coinbase/KuCoin/Binance hot wallets missing from the
    allowlist made every second customer of one exchange a "sybil sibling"."""
    from lfg_core import funding

    for account, name in (
        ("rwpTh9DDa52XkM9nTKp2QrJuCGV5d1mQVP", "Coinbase"),
        ("rEW8BjpMyFZfGMjqbykbhpnr4KEb2qr6PC", "KuCoin"),
        ("rJb5KsHsDHF1YS5B5DU6QCkH5NsPaKQTcy", "Binance"),
        ("rBtttd61FExHC68vsZ8dqmS3DfjFEceA1A", "Binance"),  # hand-listed entry kept
    ):
        assert funding.EXCHANGES[account] == name
    # The 94-wallet farm funder (#461) must never be allowlisted.
    assert "raYftkWz8dhwP3TjS2NDsW6EzFfKCizWH9" not in funding.EXCHANGES
