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


def test_every_allowlisted_funder_names_a_reviewed_custodial_operator():
    """The exemption's whole review surface is the operator list: an account is
    allowlisted only because XRPScan labels it for an exchange or custodian a
    human signed off on. A stale or broadened upstream classification (a
    project treasury, a validator, a personal wallet) must fail here rather
    than quietly weaken sybil admission, fee-cover linkage and unique_actors."""
    import json
    import re
    from pathlib import Path

    from lfg_core import funding
    from scripts.refresh_exchange_funders import REVIEWED_OPERATORS

    snapshot = json.loads((Path(funding.__file__).with_name("exchange_funders.json")).read_text())[
        "accounts"
    ]
    assert snapshot, "the snapshot must not be empty"

    unreviewed = sorted({name for name in snapshot.values() if name not in REVIEWED_OPERATORS})
    assert not unreviewed, f"unreviewed operators in the snapshot: {unreviewed}"

    classic = re.compile(r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$")
    malformed = sorted(a for a in snapshot if not classic.match(a))
    assert not malformed, f"malformed classic addresses: {malformed}"

    # Every snapshot entry reaches the live gate.
    assert set(snapshot) <= set(funding.EXCHANGES)
