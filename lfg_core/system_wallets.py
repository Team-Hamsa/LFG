"""Project-owned wallets that must never be counted as users (#413, #414).

Every consumer of this module previously resolved its exclusion set purely
from config (`BRIX_DISTRIBUTOR_ADDRESS`, `SIGNING_ACCOUNT`, …). That is
correct for the *current* wallet and wrong for every archived row: history
does not move when a config var is repointed, so the instant a distributor or
signer is rotated, all of its past activity silently reclassifies as ordinary
user activity — inflating `unique_wallets` and shifting BRIX leaderboards.

So exclusions are the union of two things:

* the **configured** wallets, resolved at call time, so a freshly-rotated
  address is excluded immediately, before anyone remembers to edit this file;
* the **durable** literals below, so a retired address stays excluded forever.

Entries are append-only. **Never remove one** — an address that has ever acted
for the project has archived rows under it permanently. Same rule
as `scripts/sourcetag_metrics.HISTORICAL_SIGNING_ADDRESSES`.
"""

from __future__ import annotations

# Distributors that have signed BRIX payouts for the project, past or present.
# rnqvoyr… was the distributor until 2026-08-20 (60,732 payouts, 1.63M BRIX to
# 182 users, 2023-06-29 → 2025-04-16); rwr84Q… replaced it. Both are project
# wallets; neither is ever a participant. Add a wallet here the day it goes
# into service, not the day it leaves — waiting until retirement is exactly
# how rnqvoyr…'s exclusion lapsed.
HISTORICAL_DISTRIBUTORS = frozenset(
    {
        "rnqvoyrWAP95mqssc9yBu6oBeayQUbrteu",  # retired 2026-08-20
        "rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ",  # in service since 2026-08-20
    }
)

# Every project wallet that must be excluded regardless of what config says.
DURABLE_SYSTEM_ACCOUNTS = frozenset(HISTORICAL_DISTRIBUTORS)


def with_durable(configured: frozenset[str]) -> frozenset[str]:
    """`configured` (whatever set a caller resolves from config) plus every
    durably-listed project wallet.

    Deliberately additive rather than a single canonical set: each consumer
    excludes a slightly different roster (the leaderboards do not exclude the
    NFT issuer, for instance, since issuer-held inventory is meaningful there),
    and the retired-wallet fix must not quietly widen any of them.
    """
    return frozenset(configured) | DURABLE_SYSTEM_ACCOUNTS
