# How we count unique users

LFG reports two numbers for SourceTag `2606160021` on mainnet:

| Metric | Value | What it counts |
|---|---:|---|
| `unique_wallets` | **717** | Distinct accounts that signed at least one SourceTag-tagged transaction, minus the project's own wallets |
| `unique_actors` | **402** | The same 717 wallets after merging wallets opened by the same funder |

The README badge shows `unique_actors`. We think it is the defensible number,
and it is the one we ask to be judged on. This page explains how we got from
717 to 402: what we removed, what we kept, and why. It also shows how the
count changes under other reasonable definitions, because the organizers'
method may differ from ours.

Snapshot: 2026-09-21, after every cached funder was re-verified against full
ledger history
([`metrics/sourcetag.json`](../metrics/sourcetag.json)), checked against the
live archive when this page was written. The code is
[`scripts/sourcetag_metrics.py`](../scripts/sourcetag_metrics.py)
(`excluded_wallets`, `dedup_by_funder`).

## Step 1: count signers, not recipients

A user is the `Account` field of a tagged transaction: the wallet whose key
signed it. Being the `Destination` of one of our transactions does not make a
wallet a user.

All transaction types count, successful or not, as long as they carry our
SourceTag. In practice almost every user wallet has at least one
`NFTokenAcceptOffer`: every mint delivers the NFT as an offer, and the user
signs the accept in Xaman or Joey Wallet to take it into their wallet.

## Step 2: remove the project's own wallets (722 → 717)

The backend signs mints, delivery offers, trait swaps (`NFTokenModify`),
burns, BRIX payouts and refunds. These transactions are about 77% of our
tagged volume and they count toward `total_tagged_txs`. Their signers are
not users, so these accounts are excluded from both user counts:

| Address | Role |
|---|---|
| `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ` | NFT issuer (backend signer via regular key) |
| `rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px` | BRIX token issuer (signs via its regular key) |
| `rnqvoyrWAP95mqssc9yBu6oBeayQUbrteu`, `rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ` | BRIX drip distributors (retired 2026-08-20, current) |
| `rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ`, `rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf` | Operator (team) wallets |

The list is append-only in code. When a signing key rotates, the old address
stays excluded, so a key change can't turn backend activity into "new users".

## Step 3: merge wallets that share a funder (717 → 402)

On XRPL a new account exists only after another account sends it the reserve.
We call the account that signed that creating transaction the wallet's
**activation funder**. We identify it from the transaction's metadata (the one
that creates the wallet's `AccountRoot`), looked up on full-history nodes. We
don't simply take the wallet's oldest listed transaction, which can be wrong:
see [Corrections](#corrections). The ledger records it permanently, so it is a cheap and stable
signal of common control. One person opening 90 wallets has to fund all 90
from somewhere.

**The rule:** wallets activated by the same non-exchange funder count as one
actor. If a funder is itself one of our user wallets, it merges with the
wallets it funded, so the parent wallet isn't counted a second time.

Funder coverage for the 717 wallets:

| Funder status | Wallets | Treatment |
|---|---:|---|
| Funded by a known exchange hot wallet | 221 | Each counts separately (see below) |
| Funded by a non-exchange account | 496 | Grouped by funder → 219 funders |

Group sizes among the 219 non-exchange funders:

```
wallets per funder   1    2   3   4   5   6   7   9   11  12  14  18  22  95
funders            167   25   8   6   2   1   1   2   1   1   1   2   1   1
```

52 funders opened 2 or more of our wallets, covering 329 wallets in total.
The largest group is **95 wallets** from one funder,
`raYftkWz8dhwP3TjS2NDsW6EzFfKCizWH9`. That funder has no label, was created
2026-05-03, and was itself funded by Bybit. The next largest groups have 22,
18 and 18 wallets.

### What we kept, and why

- **Exchange-funded wallets count individually.** Withdrawing from Binance,
  Uphold, Coinbase and similar exchanges gives every customer the same
  funder, so a shared exchange funder says nothing about common control.
  Merging on it would collapse strangers into one actor. The exemption list
  holds 717 hot wallets. It is XRPScan's labelled-exchange list, filtered to
  exchanges and custodians we reviewed by hand. A test fails if an entry
  names any other kind of operator, so an unexpected upstream label can't
  widen the exemption. Top exchanges among our users: Binance 66, Uphold 33,
  Coins.ph 22, Indodax 22, Coinbase 17, Bitrue 14, Bybit 10, KuCoin 9.
- **A wallet with no funder on record would count individually.** This is
  deliberate: a gap in our data should never make the number smaller. As of
  this snapshot there are none. Every one of the 717 wallets has a verified
  funder.
- **Wallets whose only action was accepting an NFT count.** 625 of the 717
  wallets have signed only `NFTokenAcceptOffer`. Taking an NFT into your own
  wallet is a signed, on-ledger action by the user, and the organizers
  confirmed to us that accepts count. We didn't set a minimum transaction
  count (see the sensitivity table).

### What we cut

- **Wallet farms.** Merging by funder removed 315 wallets (717 → 402). Most
  of them sit in a few large groups. 94 of the 95 wallets in the
  largest farm claimed a sponsored free mint.
- **Where the farms came from.** 646 of the 717 wallets received a
  sponsored free mint (a free mint for wallets with no earlier LFG
  transaction). After the first campaign we added two checks to free-mint
  admission (#461): a funder check that applies across all campaigns, and a
  device check. Both apply to future campaigns only. We never burn NFTs a
  farm already holds; we only stop counting those wallets. Wallets that
  never took a free mint come to 71, or 69 actors.

## How the number changes under other rules

We don't know exactly how the organizers count, so here is our wallet set
under other plausible rules, all from the same snapshot:

| Rule | Actors |
|---|---:|
| Raw distinct signers, project wallets excluded | 717 |
| Merge only funders with ≥ 10 wallets | 527 |
| Merge only funders with ≥ 4 wallets | 468 |
| Merge funders with ≥ 2 wallets, no parent/child merge | 412 |
| **Our rule (published)** | **402** |
| Our rule, counting only `tesSUCCESS` transactions | 392 |
| Our rule plus funder-of-funder chains (transitive) | 322 |
| Our rule, wallets with ≥ 2 tagged txs | 101 |
| Our rule, wallets that did more than accept an NFT / set a trust line | 61 |

On 2026-09-09 the organizers' leaderboard showed about 396 accounts for us,
when our raw count was 660. Merging by activation funder reproduced that
within a few accounts. XRPL Commons told us their count uses a
sybil-resistance filter and counts `NFTokenAcceptOffer`. That is why we chose
this rule, but we can't confirm it matches theirs exactly. On every
definition above except the two activity thresholds at the bottom, the count
is above 300.

## Reproducing it

The raw count can be checked with no access to our systems, using XRPScan's
public SQL API:

```sql
SELECT COUNT(DISTINCT Account)
FROM "platform.xrpl.transactions"
WHERE SourceTag = 2606160021
```

The result includes the project wallets from step 2. Look up each account's
activation funder (the `Account` of the transaction whose metadata creates its
`AccountRoot`, found by paging `account_tx` oldest-first on a full-history
node), then apply the step 3 rule with the exchange list in
[`lfg_core/exchange_funders.json`](../lfg_core/exchange_funders.json) plus the
eight hand-picked entries in
[`lfg_core/funding.py`](../lfg_core/funding.py). The published figure is
recomputed every night at 00:20 UTC from our transaction archive.

## Corrections

On 2026-09-21, while writing this page, we found that our funder lookup was
taking each wallet's **oldest listed transaction** as its activation. That
was wrong in two ways:

- **History-pruned node.** Since 2026-09-19 our backend has read the ledger
  through our own validator, which keeps only recent history. For a wallet
  with recent activity, its "oldest" transaction there is just the oldest one
  the node kept, not its first.
- **Transactions that only mention the wallet.** An address's history also
  lists transactions that name it without creating it. For example, another
  account can set the address as its regular key before the address exists.

Re-checking all 823 cached funders against full history corrected 32 of
them. Lookups now use full-history nodes only and take the transaction that
created the account ([#598](https://github.com/Team-Hamsa/LFG/pull/598)).
The same pass removed the BRIX token issuer from the user count. It had
signed one tagged account-settings transaction. The published figure went
from 405 to 402.
