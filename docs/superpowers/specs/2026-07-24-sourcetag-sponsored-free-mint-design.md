# SourceTag-Sponsored Free Mint Design

**Status:** Approved for implementation planning  
**Target:** Production `deploy` branch by July 31, 2026  
**Feature lineage:** Supersedes the identity-based approach in PR #209

## Summary

LFG will offer one sponsored NFT mint to each previously unseen mainnet wallet while a time-limited campaign is enabled from the Discord `/admin` panel. A wallet is previously unseen when it is not present in the unique set of accounts that have submitted a validated transaction carrying LFG's SourceTag.

The campaign is off by default, can be enabled without restarting services, expires after 60 minutes, can be stopped manually, and admits at most 100 sponsored mints per activation. The user pays neither XRP nor LFGO for the mint. After the NFT is confirmed minted, the project creates a durable obligation to burn exactly 1 LFGO from its signing wallet. The user's tagged `NFTokenAcceptOffer` transaction is the qualifying interaction for SourceTag growth.

The feature must preserve the existing paid mint path and fail closed to paid mint before a sponsorship is promised. Once a sponsored mint has crossed an irreversible boundary, recovery favors completing the user's mint and recording project debt over making the user absorb a backend failure.

## Goals

- Increase the mainnet SourceTag unique-wallet count toward 300 addresses before September 21.
- Make the feature safe to enable only during presentations, without a deployment or restart.
- Sponsor at most one mint per previously unseen wallet.
- Cap each activation at 100 admitted sponsored mints and automatically close it after 60 minutes.
- Burn exactly 1 LFGO from project inventory for every successfully minted sponsored NFT.
- Avoid showing a user a free-mint promise that later becomes a paid requirement because of a recoverable backend failure.
- Provide operators with current campaign, mint, acceptance, burn, and SourceTag metrics in Discord.

## Non-goals

- A browser-based admin dashboard. Issue #42 remains closed; Discord `/admin` is the control surface.
- User profiles or mandatory BRIX trustlines.
- Counting `SignIn` pseudo-transactions as SourceTag interactions. They are off-ledger and do not qualify.
- Making PR #209 mergeable in its current form. Its code may be used as a reference, but implementation will be based on current `main`.
- Making every collection mint free. The existing paid mint remains the default and the post-campaign path.
- Consuming the 2,223 collection slots beyond the 7,777 LFGO supply as free inventory.

## Definitions and Source of Truth

### Previously unseen wallet

A normalized mainnet classic address is eligible only when all of the following are true at reservation time:

1. It is absent from the validated transaction archive's unique `account` set for transactions whose `SourceTag` equals LFG's configured SourceTag.
2. It has no consumed sponsored-mint claim.
3. It is not a configured project, operator, issuer, or signing wallet.
4. The eligibility data source is healthy and sufficiently current according to the application's existing listener-health criteria.

Eligibility is evaluated against the committed archive snapshot available when the reservation transaction begins. A tagged transaction arriving immediately afterward does not invalidate an already-created reservation.

### Unique-wallet metric

The official metric is the count of distinct submitting accounts on validated mainnet transactions carrying LFG's SourceTag, after excluding configured project/operator accounts. NFT recipients are not counted merely because an NFT was minted or offered to them; the wallet counts after it submits the tagged acceptance transaction.

Before production activation, the existing SourceTag archive must be audited and backfilled far enough to serve as the authoritative eligibility set. The currently observed count is operationally useful but is not accepted as complete until that audit succeeds.

## Architecture

The feature adds four coordinated pieces:

1. A persistent campaign service that owns activation, expiry, capacity, and audit state.
2. An eligibility and claims service that checks the SourceTag archive and atomically reserves a wallet and campaign slot.
3. A sponsored-mint branch in the existing mint flow that skips user payment but reuses current mint, offer, and recovery machinery.
4. A durable burn worker that reconciles and completes one project-funded LFGO burn per confirmed sponsored NFT.

The Discord bot controls the campaign through service-token-protected backend endpoints. All public mint surfaces read the same backend state; no surface may decide eligibility independently.

## Persistent Data Model

All wallet-scoped records are keyed by network and normalized classic address. Mainnet and test networks must never share eligibility or claims.

### `free_mint_campaigns`

- `id`: immutable campaign identifier
- `network`: must be mainnet for production campaigns
- `status`: `active`, `stopped`, `expired`, or `full`
- `started_at`, `enabled_until`, `stopped_at`
- `started_by`, `stopped_by`: Discord operator identity
- `cap`: fixed at 100 for this release
- `created_at`, `updated_at`

Only one campaign may be active per network. Effective activity is calculated from persisted status, current time, and capacity; expiry does not depend on an in-memory timer firing. `full` is a terminal state reached after 100 sponsored NFTs consume slots. A still-active campaign with all remaining slots temporarily reserved is reported as `at_capacity` and can admit another wallet if a reversible reservation is released.

### `free_mint_claims`

- `(network, wallet)`: lifetime uniqueness key
- `campaign_id`
- `session_id`
- `status`: `reserved`, `minting`, `minted`, `offered`, `accepted`, `released`, or `failed_terminal`
- `reserved_at`, `reservation_expires_at`, `released_at`
- `mint_tx_hash`, `nft_id`, `offer_id`, `accept_tx_hash`
- `tagged_at`
- `last_error`, `created_at`, `updated_at`

A released claim can be reserved again if the wallet remains eligible. A consumed claim—one that reached a confirmed NFT mint—can never produce a second free NFT, even if offer creation, acceptance, or burn processing later fails.

### `free_mint_burns`

- `claim_id`: unique foreign key, guaranteeing one burn obligation per sponsored NFT
- `status`: `pending`, `submitting`, `indeterminate`, `burned`, or `failed_terminal`
- `memo_id`: deterministic unique identifier included in the transaction memo
- `tx_hash`
- `attempt_count`, `last_attempt_at`, `next_attempt_at`
- `last_error`, `burned_at`, `created_at`, `updated_at`

### Admin audit log

Every campaign start, stop, automatic expiry/full transition, and rejected administrative attempt records the actor, action, timestamp, campaign, and result. Existing application logging may be used if it is durable and queryable; otherwise a dedicated table is added.

## Campaign Lifecycle and Capacity

Starting a campaign creates a new record with a 60-minute `enabled_until` and a cap of 100. Starting is rejected if another campaign is effectively active. Stopping changes the campaign state immediately and prevents new reservations. Expiry and full-capacity transitions do the same automatically.

Capacity is admission-based, not merely completion-based. Within one database transaction, reservation is allowed only when:

`confirmed sponsored mints + active reservations < 100`

Here, active reservations include both `reserved` and `minting` claims that have not been safely released, while confirmed sponsored mints are consumed claims from the campaign. This prevents concurrent sessions from overbooking. Releasing a reservation before irreversible work returns its slot. Once minting reaches the irreversible boundary, the slot remains consumed. A campaign transitions permanently to `full` when its hundredth slot becomes consumed. If all available slots are only reserved, admission pauses as `at_capacity`; a safe release can reopen admission until expiry. Existing admitted sessions continue through recovery.

Reservations survive campaign stop or expiry. A user who was validly admitted while the campaign was active does not lose the offer because the operator stopped the window or the 60-minute timer elapsed.

Stale reservations may be released only when no irreversible mint work began. The cleanup operation must use persisted state and transaction evidence, not elapsed time alone.

## Eligibility and Reservation Flow

When a wallet starts a mint:

1. Resolve and normalize its classic address and network.
2. Read effective campaign state.
3. If the campaign is inactive, expired, full, or the eligibility provider is unavailable/stale, continue with the unchanged paid flow.
4. Query the SourceTag archive for the wallet and apply the project-wallet exclusion list.
5. In one application-database transaction, recheck campaign capacity and create or reacquire the wallet claim.
6. Bind the reservation to the current mint session and return sponsored pricing.

The SourceTag archive and claims database are separate stores, so the archive lookup cannot be atomically committed with the claim. The claims transaction provides one-free-mint uniqueness and capacity safety; archive eligibility uses snapshot semantics described above.

The client must not label the mint as free before reservation succeeds. After reservation succeeds, recoverable backend failures must retain the claim and resume the sponsored path rather than falling back to payment.

## Sponsored Mint Flow

The sponsored path reuses the existing `mint_one_unit` behavior and NFT offer/acceptance pipeline:

1. Confirm the reservation belongs to the current wallet and session.
2. Move the claim to `minting` before submitting irreversible work.
3. Skip the user payment payload entirely.
4. Mint one NFT using the existing project-controlled mint path.
5. Reconcile indeterminate mint submission outcomes before any retry.
6. Once the NFT is validated and its ID is known, atomically:
   - mark the claim consumed/`minted`;
   - store the mint transaction and NFT identifiers; and
   - create the unique 1-LFGO burn obligation.
7. Create a destination-locked sell offer for the eligible wallet.
8. Present a user-signed `NFTokenAcceptOffer` payload carrying LFG's SourceTag.
9. On validated acceptance, store the transaction hash and mark the claim `accepted` and `tagged`.

Offer or payload failures after mint confirmation use the existing recovery surface and may safely recreate missing downstream artifacts. They must never mint a second NFT.

## LFGO Burn Semantics

Each confirmed sponsored NFT creates exactly one obligation to burn 1 LFGO from the project's signing wallet balance. The burn is project-funded and must not block the user-facing mint or NFT acceptance.

The burn worker:

1. Claims a pending obligation with database-level mutual exclusion.
2. Builds the existing LFGO burn transaction with LFG's SourceTag and the obligation's deterministic `memo_id`.
3. Submits and waits for a validated result when possible.
4. Stores the validated transaction hash and marks the obligation `burned`.

For deterministic failures, such as insufficient project LFGO balance, the obligation remains pending with backoff and a visible operator error. For an indeterminate submission result, it moves to `indeterminate`. Before retrying, the worker searches validated signing-account history for the unique memo and verifies transaction semantics. It marks the obligation burned if found; it resubmits only after proving the original transaction did not validate. Blind resubmission is forbidden.

The project signing wallet and other excluded accounts do not contribute to the unique-user count even though burn transactions carry the SourceTag.

Admin status distinguishes:

- LFGO successfully burned
- LFGO durably owed but pending
- terminal/manual-intervention failures

Campaign activation displays the available project LFGO balance and warns if it cannot cover the remaining possible admissions. The persisted cap still controls admission; insufficient balance does not erase already-created obligations.

## Discord `/admin` Controls

The existing `/admin` application command remains protected by Discord's Administrator permission check. The admin view must additionally implement a per-interaction authorization check so every button and modal revalidates Administrator permission for the interacting user. Ephemeral visibility is not treated as authorization.

The panel adds:

- **Start sponsored mint:** creates a 60-minute, 100-slot campaign.
- **Stop sponsored mint:** immediately closes admission.
- **Refresh:** reloads authoritative persisted state.

The panel displays:

- off/active/expired/full state and countdown
- sponsored reservations and confirmed mints out of 100
- accepted/tagged sponsored wallets
- authoritative unique SourceTag wallets out of 300
- project LFGO balance
- burned and burn-pending counts
- last operator and change time

Backend campaign mutation and status endpoints require the existing Discord service token and reject other surfaces. They are idempotent where practical: repeated stop calls leave the campaign stopped, and duplicate start requests cannot create overlapping campaigns.

## User Experience

Before reservation, the UI may indicate that a limited newcomer promotion is available but must not guarantee eligibility. After successful reservation, it explicitly states:

- the mint is sponsored and costs the user neither XRP nor LFGO;
- the user must accept the NFT offer in their wallet;
- the acceptance is an on-ledger transaction and may require the wallet's normal network reserve/transaction fee behavior.

If the campaign is off or the wallet is ineligible, the existing paid flow appears unchanged. A data-source failure before reservation also presents only the paid flow, avoiding a promise that cannot be verified.

## Recovery and Failure Rules

- Campaign/database unavailable before reservation: use paid flow.
- Eligibility archive unavailable or unhealthy before reservation: use paid flow.
- Campaign expires or is stopped after reservation: honor the reservation.
- Client disconnect after reservation: resume by wallet/session; release only if no irreversible work began and the reservation is stale.
- Mint submission indeterminate: reconcile transaction/account history before retry.
- NFT confirmed but offer missing: preserve consumed claim and regenerate the offer.
- Offer exists but acceptance payload missing: regenerate the payload for the same offer.
- Burn fails: preserve user success and durable burn debt.
- Service restart: derive work from persisted claims and burn obligations; no correctness depends on process memory.
- Duplicate requests: return the existing session/claim outcome and never duplicate mint or burn.

## Security and Abuse Controls

- Mainnet-only production activation.
- Administrator permission checked at command entry and every component interaction.
- Service-token authentication on backend admin endpoints.
- Wallet normalization before all comparisons and uniqueness checks.
- Explicit exclusion list for issuer, signing, treasury, operator, and test wallets.
- Destination-locked NFT offers.
- Parameterized database access and transactional capacity checks.
- Durable audit trail for administrative changes.
- No client-provided eligibility, campaign state, price, NFT ID, or burn status is trusted.

## Metrics and Observability

Operational metrics include:

- campaign state, age, expiry, reservations, released reservations, and consumed slots
- sponsored mint attempts, confirmations, failures, and recovery backlog
- offers created and acceptances validated
- sponsored wallets newly added to the SourceTag unique set
- total authoritative unique SourceTag wallets toward 300
- burn obligations created, validated, pending, indeterminate, and terminal
- project LFGO balance and maximum uncovered obligation

Structured logs correlate campaign ID, claim ID, wallet, mint session, NFT, offer, acceptance, and burn memo without logging secrets.

## Test Strategy

### Unit and database tests

- known SourceTag wallet is ineligible
- unseen wallet is eligible once
- excluded project/operator wallets are ineligible
- released pre-mint reservation can be reacquired
- consumed claim cannot be reused
- concurrent requests for one wallet produce one claim
- concurrent requests near capacity never admit more than 100
- stop and 60-minute expiry reject new reservations but preserve existing ones
- stale reservation cleanup cannot release irreversible work
- data-source and database failures select paid flow before reservation
- paid mint behavior is unchanged
- NFT confirmation creates exactly one burn obligation
- duplicate/replayed jobs do not double mint or double burn
- indeterminate burns reconcile by memo before retry

### Admin and API tests

- `/admin` command requires Administrator permission
- every view interaction rechecks permission
- unauthorized and wrong-surface API requests are rejected
- start/stop behavior is persistent and idempotent
- restart and clock-based expiry produce correct state
- audit entries record actor and outcome

### Integration rehearsal

On staging/testnet:

1. Start a short controlled campaign without restarting.
2. Verify a known wallet receives paid flow.
3. Reserve an unseen wallet and confirm the free-payment UX.
4. Mint, create the locked offer, and accept it.
5. Verify the tagged acceptance enters the unique-wallet archive.
6. Verify one burn obligation and one validated LFGO burn.
7. Exercise restart and injected offer/burn failure recovery.
8. Stop the campaign and verify new users immediately receive paid flow.

## Release and Rollback

The feature ships to production with campaign state **OFF**. Before first activation:

1. Audit/backfill SourceTag history and record the authoritative baseline.
2. Verify the exclusion list and listener health.
3. Verify project LFGO balance and signing-account readiness.
4. Run migrations and confirm restart recovery in staging.
5. Promote through the normal `main` to `deploy` process.
6. Perform one controlled production mint, acceptance, unique-wallet update, and burn.
7. Enable a presentation campaign only after the controlled path succeeds.

Operational rollback is immediate: stop the campaign from `/admin`, which prevents new reservations without interrupting paid minting or recovery of admitted claims. Code rollback must leave the new tables intact so outstanding claims and burn obligations remain reconcilable.

## Implementation Boundary

The implementation plan will start from current `main` and compare PR #209 only for reusable concepts or tests. It will not attempt to merge PR #209 wholesale. The plan must identify the smallest coherent slices for campaign persistence, eligibility/claims, sponsored minting, burn reconciliation, admin controls, UI changes, and staged release, with review checkpoints before promotion to `deploy`.
