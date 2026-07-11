# Legacy ape faces — auto-roll on swap (#168)

_Status: approved 2026-07-11. Follow-up to #38/#145 (new ape mints get faces)
and to `2026-07-09-existing-ape-face-migration-design.md` (#146), whose §6.1
"reface my ape entrypoint" this supersedes with an automatic variant._

## 1. Problem

The 105 ape face traits (36 Eyes, 22 Eyebrows, 47 Mouth) only enter circulation
via fresh mints. Legacy apes (pre-#145) carry `Eyes/Eyebrows/Mouth = None`
on-chain, and every existing pathway preserves that: the Trait Swapper only
moves traits between two owned NFTs (and #146's guard blocks selecting a `None`
slot at all), remint faithfully preserves unswapped slots, and Equip can only
apply assets already in a Closet. Confirmed live on mainnet 2026-07-11:
editions #814/#868 swapped Accessory successfully and remain faceless by
design. Existing holders — the audience the rarity-boost arming was meant to
reward — cannot get the new face traits on apes they already own.

## 2. Decision

**Faces are rolled automatically the first time a `None`-faced ape goes
through the Trait Swapper.** No new op, no fee, no new UI on any surface
("silent surprise" — the new face first appears in the composed result the
swapper shows; see §3.4 for exactly when per path). Since a rolled ape never has `None`
faces again, this is effectively once per ape, and it is user-initiated by
construction: the owner chose to swap and signs the result.

Rejected alternatives (from #168): a paid standalone re-roll op (new surface
area for the same outcome), the trait-token route (requires harvesting the
character into a Closet — burns it), and a one-time claim flow (new UI, no
swap volume).

## 3. Design

### 3.1 Trigger and seam

In `swap_flow.run_swap_session`, immediately after
`swap_meta.swap_traits(...)` produces `new_attrs1/new_attrs2` and **before**
the `missing_layers` pre-check: for each of the session's two NFTs whose body
(`nft["gender"]`) is `ape`, any of the three face slots (`Eyes`, `Eyebrows`,
`Mouth`) whose value is missing or `"None"` is filled with a rolled value.
Ordering after `swap_traits` guarantees a real face trait moved in by the swap
itself is never overwritten; only still-empty slots are filled. Skeletons and
other bodies are untouched (their face dirs hold only `None.png`; faces are
ape-only per the affinity matrix).

Running before `missing_layers` means the rolled values get the same
layer-existence pre-check as everything else — no burn can happen against a
face whose art is missing.

### 3.2 Rolling

New helper in `lfg_core/traits.py` (alongside `select_random_attributes`,
sharing its machinery):

```
async def fill_missing_face_traits(store, body, attributes, *, conn=None,
                                   network=None, now=None, rng=random) -> bool
```

- No-op (returns False) unless `body == "ape"`.
- For each face slot in `TRAIT_ORDER` order whose current value is absent or
  `"None"`: list candidates via `store.list_values`, filter through
  `cfg.value_allowed` + `cfg.conflicts(attributes, ...)` (so `trait_config.yaml`
  exclusions/inclusions hold against the ape's existing traits, and earlier
  rolled faces constrain later ones), and pick via `rarity.weighted_pick` —
  the exact selection the mint path uses, so armed rarity boosts and floors
  apply identically. `"None"` is excluded from the candidate pool.
- Mutates `attributes` in place, replacing the `None` entry (or inserting in
  canonical position via the same list order `select_random_attributes`
  produces) and returns True if anything was rolled.
- Over-constrained slot (candidates exist but rules eliminate all): fail loud
  with `ValueError`, mirroring `select_random_attributes` — the swap session
  fails cleanly before any payment or on-chain action.

### 3.3 On-chain application — no new machinery

The rolled values are simply part of the post-swap attribute set, so the
existing flow carries them unchanged: metadata JSON + composed image upload,
then `NFTokenModify` (mutable) or burn-and-remint (legacy non-mutable — which
is what pre-#145 apes are; the remint comes back flags 25 and never needs a
burn again), offers, `SourceTag 2606160021` + provenance memos, and the #163
archive stage/promote path all pick up the new faces for free. Failure/revert
paths discard the roll along with everything else — a cancelled or failed swap
rolls nothing durable (the roll only exists in session state).

Conservation note: like a fresh mint, this creates face traits via a real
owner-signed on-chain write — it is not the fabricate-in-the-DB anti-pattern
#146 §3 rejected. `asset_census` counts Closet assets and trait tokens, not
live characters' worn traits, so economy conservation is unaffected.

### 3.4 UX

Silent. No toggles, notices, or copy changes on Discord / Activity / Telegram.
Reveal point (verified against the real surfaces at review): the pre-confirm
screen shows the NFTs' *current* images, so the rolled face is first seen in
the composed **result** — after `NFTokenModify` for mutable NFTs, or at the
**accept-offer screen** for the burn+remint path (which is what mainnet
legacy apes are; at that point the original is already burned, so declining
the offer parks the re-crafted, faced replacement with the issuer rather
than undoing the roll). Cancelling *before* signing the fee/confirm step
rolls nothing durable. This is the accepted trade-off of the
silent-surprise choice; an announcement/changelog line is the remedy if
users read it as a bug, not new UI.

## 4. Testing

- **Unit (`fill_missing_face_traits`)**: only `None`/missing slots rolled;
  non-ape bodies untouched; existing real face values preserved;
  `trait_config` exclusions respected (a config where an existing trait
  excludes a face value never rolls it); deterministic under seeded `rng`;
  `"None"` never rolled; over-constrained slot raises.
- **Swap-flow**: a legacy ape session's post-swap attributes carry all three
  face traits (and its uploaded metadata/composed basename reflect them); a
  skeleton counterpart's attributes gain nothing; a swap that moves a real
  face trait onto the ape does not re-roll that slot.
- **Revert**: an ape session failing after the roll (e.g. missing layer,
  payment timeout) leaves no on-chain or DB trace, same as today.

## 5. Out of scope

- Paid re-rolls of already-rolled faces (can layer on later if wanted).
- Skeleton face art.
- Any change to the #146 §5.1 None-swap-selection guard — it stays; it blocks
  *trading* an empty slot, we *fill* it after swap application.
