# How we count unique users

LFG reports two numbers for SourceTag `2606160021` on mainnet:

| Metric | Value | What it counts |
|---|---:|---|
| `unique_wallets` | **718** | Distinct accounts that signed at least one SourceTag-tagged transaction, minus the project's own wallets |
| `unique_actors` | **403** | The same 718 wallets after merging wallets opened by the same funder |

The README badge shows `unique_actors`. We think it is the defensible number,
and it is the one we ask to be judged on. This page explains how we got from
718 to 403: what we removed, what we kept, and why. It also shows how the
count changes under other reasonable definitions, because the organizers'
method may differ from ours.

Snapshot: 2026-09-21, after a full funder backfill
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

## Step 2: remove the project's own wallets (722 → 718)

The backend signs mints, delivery offers, trait swaps (`NFTokenModify`),
burns, BRIX payouts and refunds. These transactions are about 77% of our
tagged volume and they count toward `total_tagged_txs`. Their signers are
not users, so these accounts are excluded from both user counts:

| Address | Role |
|---|---|
| `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ` | NFT issuer (backend signer via regular key) |
| `rnqvoyrWAP95mqssc9yBu6oBeayQUbrteu`, `rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ` | BRIX drip distributors (retired 2026-08-20, current) |
| `rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ`, `rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf` | Operator (team) wallets |

The list is append-only in code. When a signing key rotates, the old address
stays excluded, so a key change can't turn backend activity into "new users".

## Step 3: merge wallets that share a funder (718 → 403)

On XRPL a new account exists only after another account sends it the reserve.
We call the sender of that first incoming payment the wallet's **activation
funder**. The ledger records it permanently, so it is a cheap and stable
signal of common control. One person opening 90 wallets has to fund all 90
from somewhere.

**The rule:** wallets activated by the same non-exchange funder count as one
actor. If a funder is itself one of our user wallets, it merges with the
wallets it funded, so the parent wallet isn't counted a second time.

Funder coverage for the 718 wallets:

| Funder status | Wallets | Treatment |
|---|---:|---|
| Funded by a known exchange hot wallet | 211 | Each counts separately (see below) |
| Funded by a non-exchange account | 489 | Grouped by funder → 212 funders |
| No funder signal (first transaction wasn't an incoming payment) | 18 | Each counts separately (see below) |

Group sizes among the 212 non-exchange funders:

```
wallets per funder   1    2   3   4   5   6   7   9   11  12  14  18  22  95
funders            159   26   9   5   2   1   1   2   1   1   1   2   1   1
```

53 funders opened 2 or more of our wallets, covering 330 wallets in total.
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
  widen the exemption. Top exchanges among our users: Binance 66, Uphold 32,
  Indodax 22, Coins.ph 21, Coinbase 16, Bitrue 14, Bybit 10, KuCoin 7.
- **Wallets with no funder signal count individually.** This is
  deliberate: a gap in our data should never make the number smaller. Every
  wallet has now been looked up, and 18 have no funder signal (their first
  transaction was not an incoming payment), so 403 can overcount by at most
  18.
- **Wallets whose only action was accepting an NFT count.** 625 of the 718
  wallets have signed only `NFTokenAcceptOffer`. Taking an NFT into your own
  wallet is a signed, on-ledger action by the user, and the organizers
  confirmed to us that accepts count. We didn't set a minimum transaction
  count (see the sensitivity table).

### What we cut

- **Wallet farms.** Merging by funder removed 315 wallets (718 → 403). Most
  of them sit in a few large groups. 94 of the 95 wallets in the
  largest farm claimed a sponsored free mint.
- **Where the farms came from.** 646 of the 718 wallets received a
  sponsored free mint (a free mint for wallets with no earlier LFG
  transaction). After the first campaign we added two checks to free-mint
  admission (#461): a funder check that applies across all campaigns, and a
  device check. Both apply to future campaigns only. We never burn NFTs a
  farm already holds; we only stop counting those wallets. Wallets that
  never took a free mint come to 72, or 69 actors.

## How the number changes under other rules

We don't know exactly how the organizers count, so here is our wallet set
under other plausible rules, all from the same snapshot:

| Rule | Actors |
|---|---:|
| Raw distinct signers, project wallets excluded | 718 |
| Merge only funders with ≥ 10 wallets | 528 |
| Merge only funders with ≥ 4 wallets | 472 |
| Merge funders with ≥ 2 wallets, no parent/child merge | 413 |
| **Our rule (published)** | **403** |
| Our rule, counting only `tesSUCCESS` transactions | 394 |
| Our rule plus funder-of-funder chains (transitive) | 327 |
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
activation funder (the `Account` of its earliest incoming `Payment` in
`account_tx`), then apply the step 3 rule with the exchange list in
[`lfg_core/exchange_funders.json`](../lfg_core/exchange_funders.json) plus the
eight hand-picked entries in
[`lfg_core/funding.py`](../lfg_core/funding.py). The published figure is
recomputed every night at 00:20 UTC from our transaction archive.
