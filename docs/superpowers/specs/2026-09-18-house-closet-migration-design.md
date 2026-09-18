# House Closet — move the project's trait stock into the Closet market

**Status:** approved (house wallet, 2026-09-18)
**Date:** 2026-09-18
**Issue:** #548 (the project-stock half; user listings stay open there)
**Depends on:** Closet Market #443 (live in prod since 2026-09-18), Browse merge #550

## Problem

Traits sell two ways. NFT trait listings are trait tokens with a native sell
offer. Closet asks are off-ledger listings straight out of a seller's Closet
(#443). Browse shows both (#550), but only Closet orders match each other: a
Closet bid never fills an NFT listing, even a cheaper one. On 2026-09-18 a
10 BRIX bid for Background: XMEME Green sat unfilled next to a 5 BRIX NFT
listing of the same trait.

Almost all NFT trait listings are the project's own stock:

| mainnet, 2026-09-18 | |
|---|---|
| Trait tokens held by the app wallet `rLfgoMint…` | 278 |
| … of those with a live in-app listing | 275 (52 are value `None`) |
| … unlisted | 3 |
| Listing prices (excluding `None`) | 5 to 12,038 BRIX, 226,953 BRIX in total |
| Live NFT trait listings from users | 5, from 3 wallets |

The app wallet is the issuer and cannot own a Closet (#383), so it can't post
Closet asks. The fix is a **house wallet**: a project wallet that is not the
issuer, owns a Closet, and holds the stock as Closet asks.

## Decisions

- **The house wallet is an existing vanity wallet the operator already controls.**
  Nothing creates a wallet.
- **Its address and seed live in `.env`** (`CLOSET_HOUSE_WALLET`,
  `CLOSET_HOUSE_SEED`). The seed signs only the one-time setup: the BRIX trust
  line and the Closet accept. Asks need no signature and the app wallet pays
  out sales, so the house never signs anything after setup.
- **Scope is the app wallet's listed trait tokens.** Each is burned into the
  house Closet. Non-`None` units are relisted at their current price; the 52
  `None` units are held (the Closet market refuses empty slots, #516 D5). The 3
  unlisted tokens stay as NFTs.
- **Standing bids fill at the bid's price.** An ask posted under a higher open
  bid crosses at once, and the resting order sets the price (`cross_incoming`).
  The XMEME Green ask at 5 would fill the 10 BRIX bid at 10.

## Design

### 1. House wallet identity — `lfg_core/house_closet.py`

`house_wallet()` returns the configured address plus a signing `Wallet`, or
raises with a message naming the problem:

- `CLOSET_HOUSE_WALLET` must be a valid classic address.
- `CLOSET_HOUSE_SEED` must derive that same address. A regular-key setup is not
  supported: the error says so rather than signing for the wrong account.
- The address must not be a system wallet: the app/signing account, the NFT or
  BRIX issuer, the LFGO issuer, the BRIX distributor or the AMM account.

`house_address()` needs only the address, for read-only status and exclusions.

### 2. Deposit into another wallet's Closet — `economy_flow.run_deposit`

`DepositSession` gains `credit_to: str | None`. `owner` stays the token's
on-ledger holder (checked, then burned); the Closet credited is
`credit_to or owner`. Every Closet check (active, mirror pending, mirror behind),
the contents read and the sync use that Closet owner. The per-owner lock keys on
the Closet owner, since that is the Closet being rewritten. The journal record
gains `credit_to` so a `deposited_pending_closet` recovery credits the right
Closet. With `credit_to` unset nothing changes.

This reuses Deposit's ordering, fail-closed checks and journaling instead of a
second burn path. It is supply-neutral: the unit leaves `trait_tokens` and
enters `closet_assets`, and the census counts both.

### 3. `scripts/house_closet.py`

Shaped like `scripts/closet_market_setup.py`: read-only by default, `--apply`
steps behind a typed-network confirmation, `--network` must match
`XRPL_NETWORK`.

- **No flags: status.** House config and on-ledger account, BRIX trust line,
  Closet state, and the migration plan with totals, the `None` holds, and each
  standing bid that would fill immediately (with its fill price).
- **`--apply-setup`.** Idempotent. The account must already be funded. Sets
  the BRIX trust line (limit `BRIX_TRUSTLINE_LIMIT`) if it is missing or lower.
  Mints the house Closet if needed (`ensure_closet`), accepts its offer with
  the house seed, then promotes it to active (`confirm_accept`). Both house
  transactions carry the SourceTag and provenance memos (initiator `backend`,
  platform `backend`, actions `trustset` / `accept-offer`).
- **`--migrate`.** Dry run: prints the plan, writes nothing.
- **`--migrate --apply`.** Runs the two phases below.

### 4. Migration

**Plan.** Every live in-app trait listing (`market_listings`, kind `trait`, no
`destination`, BRIX price) whose seller is the app wallet and whose token the
app wallet still holds in `trait_tokens`. Action is `list` (price = the listing's
`amount_brix`, normalized) or `hold` for value `None`.

**Plan file** `reports/house_migration_<network>.json` (gitignored), keyed by
`nft_id`, rewritten atomically after every item. A re-run resumes from it and
adds newly listed stock. Status per item:

| status | meaning | re-run |
|---|---|---|
| `planned` | not started | deposit it |
| `failed` | Deposit failed before the burn; nothing changed | retry |
| `needs_attention` | burned (or burn outcome unknown) but the credit didn't complete | never retried; resolve from the Deposit journal |
| `skipped` | the app wallet no longer holds the token (sold meanwhile) | none |
| `held` | deposited, value `None`, not listed | none |
| `deposited` | deposited, waiting for its ask | list it |
| `listed` | ask posted | none |

**Preconditions, checked before anything is written:** valid house config;
house Closet active; house BRIX trust line with limit ≥ `BRIX_TRUSTLINE_LIMIT`;
Closet market enabled; no item in `needs_attention`.

**Phase 1 — deposit.** Runs only if an item is `planned` or `failed`, and then
refuses while the house has a live ask or an unfinished fill: the service
rewrites the house Closet when it settles a fill, and a second process rewriting
the same Closet token can lose an update. For each such item:

1. Check on-ledger that the app wallet still holds the token. If it doesn't,
   mark it `skipped`.
2. `run_deposit(owner=app wallet, credit_to=house)`. The burn also deletes the
   token's sell offer on-ledger (XLS-20: up to 500 offers per burned token).
3. On success (including a credit whose DB mirror lags), close the listing row
   as `cancelled` (the listener does not close listings on a burn; the nightly
   market sweep would, a day later), then mark the item `deposited` (`list`) or
   `held` (`None`).
4. On failure, record the error and the journal id and stop. The item is
   `needs_attention` if the session recorded a burn hash or an unknown-outcome
   transaction, otherwise `failed`.

Before each Deposit the item records its `attempt` (the Deposit's journal id)
in the plan file, and clears it once the Deposit returns with a known outcome.
An `attempt` still set on the next run means a run died mid-item; that journal
decides: a completed Deposit (`complete` / `complete_pending_mirror`) is
recorded without burning again; a journal that shows no burn (`failed_burn`) is
retried normally; a plain `failed` journal with no burn or pending hash and the
token still held is retried; anything else, including a missing journal
(journal writes are best-effort, so one can burn without leaving a record), is
`needs_attention`.

**Phase 2 — list.** Runs only when no item is `planned`, `failed` or
`needs_attention`. For each
`deposited` item: `create_ask(owner=house, price)`, then `cross_incoming`. Record
the order id and any fill id, and mark the item `listed`. The service's
two-minute sweep settles any fills. Phase 2 never touches the Closet token, so it
cannot race the service.

### 5. Exclusions

The house wallet is a project wallet. Add it to the leaderboard system accounts
(`_lb_system_accounts`, so it doesn't top the BRIX boards with sale proceeds)
and to the SourceTag metrics' operator wallets (so its two setup transactions
don't count as a user). Both read `CLOSET_HOUSE_WALLET` at call time; for a
later rotation, every house address also goes into the append-only
`system_wallets.HISTORICAL_HOUSE_WALLETS` the day it goes into service (#414),
and setup and the migration refuse a house that isn't listed there.

## Side effects

- Browse keeps showing the same traits at the same prices, now with the house as
  seller. The service's 60 s listings cache can show a burned NFT row briefly;
  Buy on it gets the existing 410 stale response.
- Sale proceeds (price minus the 7% market fee) land in the house wallet as BRIX.
- `None` units sit in the house Closet, unlisted.

## Rollout

1. Fill `CLOSET_HOUSE_WALLET` / `CLOSET_HOUSE_SEED` in the staging `.env` (fund
   the same wallet from the testnet faucet), run `--apply-setup`, then
   `--migrate` (dry run) on testnet.
2. Mainnet: fund the house wallet, fill the prod `.env`, `--apply-setup`,
   `--migrate`, review the plan (especially the standing bids it fills), then
   `--migrate --apply`.
3. Check Browse and `scripts/audit_trait_economy.py` (census unchanged).

## Testing

- Identity: mismatched seed, system-wallet address, missing values.
- Deposit `credit_to`: credits the other Closet, checks and burns the holder's
  token, locks the Closet owner, journals `credit_to`; default path unchanged.
- Plan: list vs hold, unlisted and external rows skipped, prices normalized.
- Apply: each precondition fails closed; phase 1 refused while the house has a
  live ask or fill; a failure stops the run; a pre-burn failure is retried and a
  post-burn one is `needs_attention` and never retried; resume after a crash; a
  sold token is skipped; listing rows closed; phase 2 asks at the old prices; a
  standing bid crosses at the bid price.
- Setup: TrustSet and accept built for the house account with SourceTag and memos.
- Exclusions: the house wallet is in both lists; setup and the migration refuse
  a house missing from the durable roster.
- Crash resume: a Deposit that completed before the plan save is recorded, not
  burned again; one that died between the burn and the credit, or left no
  journal, is `needs_attention`; a known pre-burn refusal is still retried.

## Out of scope

- User NFT listings (5): moving them needs an owner opt-in. #548 stays open.
- Closing listings on any burn in the listener: a general gap, filed separately.
- The staging `CLOSET_MARKET_ENC_KEY` is not a valid Fernet key (ops fix).
