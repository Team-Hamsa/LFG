# Re-enable the burnable flag on the minter

**Date:** 2026-06-24
**Status:** Approved (brainstorming → spec)
**Related:** Dress-Up Trait Economy (#46), Phase 2 harvest (#64), Phase 4 (#66)

## Problem

The dress-up trait economy's **Harvest** op burns a live character to drop its
assets into the owner's Bucket. `lfg_core/trait_economy.py:can_harvest()`
requires the character's NFToken to be **burnable** — the issuer cannot burn a
mutable-only token, so a non-burnable character is "equip-only until re-minted".

Since the Dynamic NFTs amendment, the regular minter was switched to
`NFT_FLAGS = 24` (`tfTransferable` + `tfMutable`, **not** burnable) so trait
swaps update tokens in place via `NFTokenModify` instead of burn-and-remint.
Side effect: **every NFT minted from the live minter is non-harvestable.** If we
shipped today, nothing minted going forward could enter the harvest economy.

## Goal

All NFTs minted from here on are **burnable + transferable + mutable**
(`flags = 25`), so they are simultaneously:

- **harvestable** (issuer can burn them — `lsfBurnable`),
- **swappable in place** (still mutable — `tfMutable`, so the swap flow keeps
  using `NFTokenModify`, never burn-and-remint),
- **transferable** (`tfTransferable`).

This is exactly the value `ECONOMY_NFT_FLAGS` already uses (`25`); the change
makes the regular minter produce economy-compatible characters.

## Scope

**Forward-only.** NFToken flags are fixed at mint time and cannot be flipped
retroactively. Tokens already minted at `flags = 24` stay non-harvestable; this
change affects only mints from the change onward. Seeding harvestable test
characters for the *existing* set is out of scope — use
`scripts/economy_bootstrap_char.py` for that.

## Why this is safe for trait-swaps

The swap state machine classifies tokens by **mutability**, not burnability
(`lfg_core/swap_flow.py:366`):

```python
modify_items = [it for it in items if it["nft"].get("mutable")]
burn_items   = [it for it in items if not it["nft"].get("mutable")]
```

`24 → 25` only **adds** the `lsfBurnable` bit; the token stays mutable, so it
still lands in `modify_items` and is swapped in place. No swap behavior changes.

The legacy burn-and-remint branch remints "as mutable, per `NFT_FLAGS`"
(`swap_flow.py:7`). Post-change those remints come back at `flags = 25` too —
burnable+mutable — so a swapped legacy token becomes harvestable. This is
consistent and desirable.

## Design

Introduce named NFToken flag-bit constants in `lfg_core/config.py`, mirroring
the existing `NFT_FLAG_MUTABLE = 0x0010` convention in
`lfg_core/swap_meta.py:161`, and compose `NFT_FLAGS` from them so the intent is
self-documenting:

```python
# XLS-20 / Dynamic NFTs NFToken flag bits
NFT_FLAG_BURNABLE = 0x0001      # lsfBurnable — issuer may burn (required for Harvest)
NFT_FLAG_TRANSFERABLE = 0x0008  # tfTransferable
NFT_FLAG_MUTABLE = 0x0010       # tfMutable — Dynamic NFT, in-place NFTokenModify

# 25 = burnable + transferable + mutable. Burnable so harvested characters can
# be burned by the issuer; mutable so trait swaps update in place (never
# burn-and-remint). Env override still wins.
NFT_FLAGS = int(
    os.getenv("NFT_FLAGS", str(NFT_FLAG_BURNABLE | NFT_FLAG_TRANSFERABLE | NFT_FLAG_MUTABLE))
)
```

### Touch points

1. `lfg_core/config.py:68` — add the flag-bit constants; default `NFT_FLAGS`
   becomes `25` via the composed expression. Update the surrounding comment.
2. `main.py:142` — its own local `NFT_FLAGS = int(os.getenv("NFT_FLAGS", "24"))`
   (used by the `/letsgo` mint at `main.py:367,377`) → default `"25"`.
3. `.env` — set `NFT_FLAGS=25` (live source of truth; both code spots read env
   first). Update the `.env` template note in `CLAUDE.md`.
4. Docs — update `CLAUDE.md` XRPL Integration section ("new mints are NOT
   burnable") to reflect that new mints are burnable+transferable+mutable, and
   note that trait swaps still modify in place (mutability, not burnability,
   selects the swap path).

### Deliberately unchanged

- `lfg_core/swap_flow.py` — branches on `mutable`; no edit needed.
- `ECONOMY_NFT_FLAGS` (`config.py:147`) — stays a **separate** named constant
  (also `25`). Keeping economy assemble/bootstrap mints explicitly flagged
  preserves intent even though the value now coincides with `NFT_FLAGS`. No
  behavioral change, no churn.

## Implications (accepted)

- **Mainnet too:** the issuer wallet can burn any minted token (`lsfBurnable`).
  This is inherent to the harvest economy and already true for economy mints;
  the change makes it uniform across all mints.
- **No migration:** pre-change tokens remain non-harvestable by design.

## Testing

- **Unit:** assert `config.NFT_FLAGS & NFT_FLAG_BURNABLE` is set, and that the
  default composes to `25`.
- **Unit / guard:** a flag-`25` token normalized via
  `swap_meta.normalize_nft(..., flags=25)` reports `mutable == True`, so swap
  classification still routes it to `modify_items` (not `burn_items`).
- **Mint-path:** assert the `/letsgo` mint passes `flags=NFT_FLAGS` (= `25`)
  into the `NFTokenMint` for both the issuer-self and separate-issuer branches.
- **Manual / testnet E2E:** mint one token, confirm via `account_nfts` / the
  on-chain index that `Flags` includes the burnable bit; run a trait swap on it
  and confirm it took the modify path; run Harvest on it and confirm
  `can_harvest` passes.

## Out of scope

- Re-minting existing flag-24 testnet/mainnet tokens.
- Any change to `ECONOMY_NFT_FLAGS`, `BUCKET_NFT_FLAGS`, or the swap flow.
- Phase 4 (#66) tradeable trait NFTokens — tracked separately.
