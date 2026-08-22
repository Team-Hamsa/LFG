# Royalty-refund campaign — paying sellers to settle on our rails

**Status:** proposed
**Date:** 2026-08-22
**Depends on:** bid-to-seller surface (this is pointless without it), #283
**Related:** the issuer regular key (`MAINNET_REGKEY_SEED`), `brix_claims` (the
pattern this copies)

## Problem

A seller has no reason to prefer our accept button over the broker's. Both
move the NFT; the broker's is where they already are.

## The lever

Every transferable token we mint carries `TransferFee = 7000` (7%, units of
1/100,000), and that royalty is paid **to the issuer account on every
secondary sale** — including sales we had nothing to do with.

Refunding it to sellers who settle through LFG costs us nothing we would
otherwise have: it returns money we hold only *because* that sale happened. Net
cost is foregone royalty revenue on LFG-routed sales, not new spend.

The seller-side arithmetic is the whole pitch:

| Route | Seller nets |
|---|---|
| xrp.cafe | `ask - 1.589% broker - 7% royalty` ~= **91.4%** |
| LFG accept, refund active | no broker fee, royalty returned = **100%** |

~8.6 points is enough to change behavior.

## The volume effect

The refund is itself a `Payment`, which makes it the only piece of this
initiative that registers in `xrp_payment_volume` — `scripts/sourcetag_metrics.py`
values tagged `tesSUCCESS` XRP `Payment`s and nothing else. A refunded,
LFG-routed sale therefore produces three tagged transactions where a
broker-settled one produces one, and the third one carries value.

## Design

### Eligibility

A refund is owed when **all** hold:

1. A `tesSUCCESS` `NFTokenAcceptOffer` carrying `SourceTag = 2606160021`,
2. signed by the **seller** (the token's owner-of-record before the tx),
3. on a token of ours,
4. that actually delivered a royalty to the issuer, and
5. that passes the anti-abuse rules below.

Nothing about the listed price or the app's session state enters this. The
whole judgement is made from the validated transaction and its `meta`.

### The refunded amount is derived from `meta`, never from the price

**Requirement, not preference.** The refund equals the XRP the issuer account
actually received in that transaction's metadata, read off the issuer's
balance delta. If that cannot be determined, no refund is issued and the row
is parked for a human — we never pay out against a number we did not observe
on-ledger.

This rules out paying on a partial fill, an IOU-denominated sale, an
unexpected fee path, or a forged memo. Consequences: IOU-denominated sales are
out of scope for v1 (the royalty arrives as an issued amount, not XRP), and a
sale where the issuer received nothing refunds nothing.

### Durability, ordering, and recovery

This moves real money, so it copies `brix_drip` exactly rather than inventing
a posture:

- A `royalty_refunds` table keyed on the **accept tx hash** — a PRIMARY KEY,
  so a re-derivation or a listener replay can never pay twice. sqlite enforces
  the invariant, not application logic.
- Order: record the owed refund → submit the `Payment` → record the outcome.
- Every payout carries `LastLedgerSequence` with margin, so failure is
  *decidable*: absent from the issuer's `account_tx` **and** past that ledger
  means it can never validate. Absence alone is never treated as failure.
- An **indeterminate** outcome leaves the row open and unpaid-but-bound. It is
  never retried blind.
- A `scripts/recover_royalty_refunds.py`, run at startup and on demand,
  resolves open rows by looking for the refund's memo in the issuer's
  `account_tx` — the same shape as `recover_brix_claims.py`.

### Signing

The refund is a `Payment` from the **issuer** account, signed with the regular
key. Per the repo's standing gotcha, `Account` must be set to the issuer
explicitly — `xrpl_ops` otherwise derives it from the seed, which is wrong for
regkey ops.

It carries `SourceTag` and provenance memos like every other transaction here:
`action=payment`, plus a refund memo naming the accept tx hash, which is what
makes recovery decidable.

### Anti-abuse — required for v1

Royalty-in equals refund-out, so two colluding wallets can trade a token back
and forth generating unlimited tagged volume for the cost of network fees. It
is free for us in XRP terms and disqualifying if it shows up in numbers we
present to a hackathon. These are not follow-ups:

- **Distinct counterparties.** No refund where buyer and seller have
  transacted this token with each other before, or share an LFG identity.
- **Per-wallet cap** per rolling window, on both count and total XRP refunded.
- **Per-token cooldown.** One refunded sale per token per window.
- **Minimum sale price.** Below it, no refund — kills dust-churn.
- **System wallets excluded**: the issuer, `config.SIGNING_ACCOUNT`, the
  distributor, and `SPONSORED_MINT_EXCLUDED_WALLETS`.
- **A campaign budget ceiling** in XRP. When it is exhausted the campaign
  stops paying and says so; it does not accrue debt.

Every declined refund records its reason. An audit that cannot say *why* a
seller was not paid is an audit that will be argued with.

### Flag and lifecycle

`ROYALTY_REFUND_ENABLED=0` by default, with the campaign's caps and budget as
env knobs. Off means eligible sales are still **recorded** (so the campaign can
be evaluated before it is armed) but nothing is paid. This matches how every
other campaign here ships.

### Operator surface

- `scripts/royalty_refund_report.py` — owed vs. paid vs. declined-with-reason,
  budget burn-down, and refunds-by-wallet, which is where wash-trading shows
  up first.
- A nightly audit that cross-checks refunds paid against royalties received,
  exiting non-zero on any refund exceeding its sale's observed royalty.

## What this does not do

- It does not refund broker-settled sales. The point is to move sellers off
  them.
- It does not refund the buyer anything.
- It does not cover IOU-denominated sales in v1 (see above).
- It does not change `TransferFee`. The royalty is still collected on every
  sale; this only returns it on qualifying ones.

## Risks

- **Wash trading** — the central risk, mitigated above and monitored by the
  by-wallet report. Assume someone will try.
- **Optics.** "We refund our own royalty to sellers who use our button" must
  be stated plainly in the campaign copy. Anything less reads as manufactured
  volume when someone reconstructs it from the ledger, which they can.
- **Issuer balance.** Refunds are paid from the account that collected them,
  but timing can drift; the report's burn-down is the number to watch.
- **A refund that fails after a successful sale** leaves a seller promised
  money they did not get. Hence decidable failure and startup recovery.

## Testing

- Amount derivation from real `meta` fixtures: XRP sale, IOU sale (declined),
  brokered sale (declined), zero-royalty sale (declined).
- Double-pay impossibility: same accept hash inserted twice.
- Decidable failure: absent payout before `LastLedgerSequence` stays open;
  after it, fails; found-by-memo marks paid.
- Every anti-abuse rule, each with a case that trips it and one that does not.
- Budget exhaustion stops payment and records the reason.
- Flag off records eligibility and pays nothing.
