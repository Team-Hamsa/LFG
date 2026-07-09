# Existing faceless apes — scope & decision (#146)

_Status: decided. Follow-up to #38 (new ape mints get faces, PR #145). This
supersedes an earlier draft that centered on a burn+remint migration — that
approach is **rejected** (see §4)._

## 1. Problem

New ape mints now draw real Eyes/Eyebrows/Mouth (PR #145). The **200 apes
already minted on mainnet** still carry `Eyes/Eyebrows/Mouth = None` on-chain.
They render fine (compose skips `None`), but:

- they have no face trait to harvest/extract/trade in the (launch-disabled)
  trait economy, and
- in the peer Trait Swapper, a `None` face slot is a *hazard*: swapping it hands
  `None` to the counterparty NFT, deleting a face it had.

The original idea — "assign faces to existing apes **in the DB**" — does not
work. See §3.

## 2. On-chain reality (verified 2026-07-09)

| Fact | Value |
|---|---|
| Live original apes | 200, across 59 wallets (issuer holds 0) |
| Flags | **9 = burnable + transferable, NOT mutable** |
| Metadata/image | IPFS (original PFP art, not app composites) |

**Not mutable** ⇒ no `NFTokenModify`; the only way to change an ape's traits is
**burn + remint**. **Issuer-minted + burnable** ⇒ the issuer *could* burn them
without consent — but that is exactly what we refuse to do (§4).

## 3. Why "just change the DB / read the DB in the swapper" doesn't work

Two separable ideas hide in this:

1. **Change the READ SOURCE** (live URI fetch → our listener-fresh
   `onchain_<net>.db` mirror). ✅ Legitimate and worthwhile — faster, and removes
   the "BunnyCDN pull-zone down → zero swappable NFTs" fragility. The index is
   still ledger-truth. Tracked as an optional optimization (§6.2).
2. **Change the CONTENT** (write faces into the DB that aren't on-chain). ❌ The
   swap **writes its result back to the ledger** (modify / burn+remint). If the
   app believes an ape has eyes the chain says are `None`, a peer swap hands a
   *real* fabricated trait to the counterparty and bakes it on-chain —
   **inventing a trait from nothing** (breaks the economy's
   `census == genesis + Σ supply_changes` conservation) and diverging from what
   Xaman / marketplaces / the on-chain economy all see. The ledger is the shared
   source of truth; we can make our mirror faster, not make it lie.

So a face can only become real via an **on-chain write**, and for these
non-mutable tokens that write is a burn+remint.

## 4. Rejected: issuer-driven mass migration

Burning 200 users' NFTs and re-offering replacements is rejected:
**the project must never burn NFTs out of a user's wallet — all burns must be
user-initiated where possible.** A forced migration also changes provenance
(IPFS→CDN) for holders who didn't ask, spikes issuer reserve (~40 XRP of pending
offers), needs outreach to 59 owners, and strands apes whose owners never accept.

## 5. Decision

**No migration.** Two concrete pieces of work instead:

### 5.1 Guard `None` out of the swap pool — DONE (this PR)

The peer swapper now rejects any selected slot that is empty (`None`) on either
NFT (`swap_meta.none_swaps`, enforced in `handle_swap_start` after the body-
affinity gate). You can't trade a slot you don't fill, so a faceless ape can
never hand `None` to a partner. Small, on-chain-truth-preserving, no burns.

### 5.2 Faces are added only by the OWNER, when they want

Because these apes are non-mutable+burnable, an owner who wants a face gets it
through a **normal, owner-signed swap** that adds it (the existing legacy-
burnable path in `swap_flow.py` already burns+remints on the owner's signature).
That is user-initiated by construction — no backend batch job, no forced burns.
No new code is required for launch; a dedicated "give my ape a face" entrypoint
(seeding a swap with the three face slots) is an optional future nicety.

## 6. Optional, non-blocking follow-ups

1. **"Reface my ape" entrypoint** — a one-tap owner-initiated swap that fills the
   three empty face slots (rarity-weighted; owner previews & signs). Pure UX
   sugar over the existing swap path.
2. **Swap read-source → index mirror** — point `swap_meta.load_wallet_nfts` at
   `onchain_<net>.db` `attributes_json` instead of live-fetching each URI.
   Faster, CDN-independent, still ledger-truth. Orthogonal to faces.

## 7. Outcome for #146

Close the "migrate existing apes" framing. Existing apes keep `None` faces until
their owner opts to add one via a normal swap. The pool-degradation risk is
handled by §5.1. Reminted apes (from any owner swap) are flags=25
(burnable+transferable+**mutable**), so they never need a burn+remint again.
