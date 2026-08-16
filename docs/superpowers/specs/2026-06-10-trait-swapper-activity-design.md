# Trait Swapper × Discord Activity — Design

Date: 2026-06-10 · Branch: `webapp-activity` · Autonomous /goal run

## Goal

Combine the Trait Swapper bot (github.com/joshuahamsa/Trait-Swapper) with the
Discord Activity webapp: users pick two of their LFGO NFTs inside the Activity,
swap selected traits, and receive the re-crafted NFTs (old ones burned,
new ones reminted and offered back).

## Source analysis (Trait-Swapper repo)

The original is a standalone discord.py bot (`main.py` + `helpers.py`):

1. `/swap` lists the user's wallet NFTs issued by `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ`
   (AccountNFTs over WebSocket), decodes each hex URI, fetches metadata
   (IPFS → dweb.link or BunnyCDN), and normalizes attributes:
   - fixes the `Accesory` typo, fills missing trait_types with `None`,
   - orders by `[Background, Back, Body, Clothing, Mouth, Eyebrows, Eyes, Head, Accessory]`,
   - moves Angel-Wings values from Accessory to Back,
   - derives season from the `#N` in the name (1–707 / 708–2121 / 2122–3535).
2. User picks two NFTs; a **gender check** requires both to share the same body
   class (Straight→male, Curved→female, Ape→ape, else skeleton); Body itself
   is never swappable.
3. User picks traits to swap from `[Background, Back, Clothing, Mouth,
   Eyebrows, Eyes, Head, Accessory]`; selected traits are exchanged, the rest kept.
4. Images are recomposed from gender-specific layer dirs
   (`<gender>/<TraitType>/<Value>.png|.gif|.mp4`) via ffmpeg overlays
   (videos get audio remuxed; "top traits" like Laser eyes render last).
5. Upload image/video + new metadata JSON (burnCount+1) to BunnyCDN under
   `LFGO/<nftNumber>/<nftNumber>_<burnCount>.<ext>`.
6. Burn both old NFTs (issuer wallet, `owner=` user), remint with taxon 1760,
   create sell offers back to the user for **10 BRIX**, send XUMM accept links.

Hardcoded secrets in the original (XUMM keys, Discord token, addresses) are
**not** carried over; everything is env-driven.

## Approach

Port the pipeline into `lfg_core` (same pattern as the mint flow) and surface
it in the existing Activity webapp — no second app, no Node.

- `lfg_core/swap_meta.py` — NFT listing + metadata fetch/normalization +
  gender/season helpers (pure/async, no Discord).
- `lfg_core/swap_compose.py` — recompose a swapped NFT from gender layer dirs
  with ffmpeg (PNG, GIF→mp4, MP4 with audio; ffmpeg-only, no moviepy/cv2).
- `lfg_core/xrpl_ops.py` — add `get_account_nfts`, `burn_nft`,
  and an `amount=` parameter on `create_nft_offer` (BRIX-priced offers).
- `lfg_core/swap_flow.py` — `SwapSession` state machine:
  `composing → uploading → burning → minting → creating_offers →
  offers_ready | failed`. In-memory, polled like mint sessions.
- `webapp/server.py` — `GET /api/nfts` (wallet inventory, normalized),
  `POST /api/swap` (validates ownership, gender match, swappable traits;
  spawns background task), `GET /api/swap/{id}`.
- `webapp/client/` — Swap panel: NFT picker (two selections with previews),
  trait checkboxes showing `value1 ↔ value2`, confirm, progress, two accept QRs.

## Config additions (env, with original values as defaults)

| Var | Default |
|---|---|
| `SWAP_ISSUER_ADDRESS` | `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ` |
| `SWAP_TAXON` | `1760` |
| `SWAP_LAYERS_DIR` | `swap_layers` (contains `male/ female/ ape/ skeleton/`) |
| `SWAP_CDN_FOLDER` | `LFGO` |
| `SWAP_OFFER_CURRENCY_HEX` | BRIX hex |
| `SWAP_OFFER_ISSUER` | `rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px` |
| `SWAP_OFFER_AMOUNT` | `10` |
| `SWAP_MAX_NFT_NUMBER` | `3535` |

## Error handling

- Burn succeeds but mint fails → session `failed` with explicit error naming
  the burned NFT ids (admin recovery); never burn before both images uploaded.
- Missing layer asset → fail before any burn.
- One active swap session per user (409 otherwise), same as mint.

## Testing

Smoke tests (no network): metadata normalization (typo, missing traits,
Back fix), gender check, trait-merge logic, swap session happy path and
pre-burn failure path with xrpl/xumm/bunny/compose stubbed.
