# XRPL Actions: payment-first atomic mint over BatchV1_1 (design)

**Date:** 2026-07-18
**Status:** Approved for implementation
**Goal:** Turn an LFG mint link shared on X into an XRPL-native action that a
returning user completes with one Xaman approval. On ledger, payment must run
first, followed by mint/offer and acceptance, with all three effects succeeding
or reverting together.

## Decision

Build a draft, wallet-neutral **XRPL Actions** application standard and ship its
first reference implementation in LFG. The mint action uses existing
`NFTokenMintOffer` behavior plus the corrected `BatchV1_1` amendment. It does
not require a new ledger transaction type or another amendment.

The atomic group is fixed and ordered:

1. buyer `Payment` to the same destination and for the same amount used by the
   current mint flow;
2. issuer `NFTokenMint` with `Amount: "0"` and `Destination: <buyer>`, which
   mints the NFT and creates a destination-locked zero-price sell offer;
3. buyer `NFTokenAcceptOffer`, which consumes that offer and transfers the NFT
   to the buyer.

The outer `Batch` uses `tfAllOrNothing`. The buyer signs the complete Batch
once in Xaman. LFG signs only for the issuer's mint leg. A buyer cannot approve
the payment without also approving the exact mint and accept legs, cannot strip
or alter the issuer leg, and cannot leave LFG with a successful payment if
either later leg fails.

This deliberately preserves the product invariant requested for LFG:

```text
PAY -> MINT + DESTINATION-LOCKED FREE OFFER -> ACCEPT
                  one ALLORNOTHING Batch
```

## Verified ledger and wallet facts

- XLS-52 / `NFTokenMintOffer` extends `NFTokenMint` with `Amount`,
  `Destination`, and `Expiration`. Supplying `Amount` creates a sell offer in
  the mint transaction; it does not transfer ownership by itself.
  <https://xls.xrpl.org/xls/XLS-0052-NFTokenMintOffer.html>
- The NFT offer key is deterministic. `rippled` computes it from the offer
  owner's AccountID and the `Sequence` or `TicketSequence` of the transaction
  that creates it. `NFTokenMint` passes its own sequence proxy into the shared
  offer-creation helper.
  <https://github.com/XRPLF/rippled/blob/develop/src/libxrpl/tx/transactors/nft/NFTokenMint.cpp>
  <https://github.com/XRPLF/rippled/blob/develop/src/libxrpl/ledger/helpers/NFTokenHelpers.cpp>
- Batch applies inner transactions in array order over a running Batch view.
  Therefore the accept leg can see and consume the offer created by the mint
  leg immediately before it.
  <https://github.com/XRPLF/rippled/blob/develop/src/libxrpl/tx/apply.cpp>
- `ALLORNOTHING` commits all inner effects or none. Inner transactions carry
  zero fee, an empty `SigningPubKey`, no individual signature, and the
  `tfInnerBatchTxn` flag. All involved accounts sign the mode and the complete
  ordered set of inner transaction hashes.
  <https://xrpl.org/docs/concepts/transactions/batch-transactions>
- Xaman supports Batch sign requests, an enforced signer, native
  `BatchSigners`, and multi-account signature collection. A Batch can be sent
  through the normal payload API.
  <https://docs.xaman.dev/concepts/special-transaction-types/batch-multiple-inner-signers>
- The original `Batch` amendment ID
  `894646DD5284E97DECFE6674A6D6152686791C4A95F8C132CCA9BAF9E5812FB6`
  is obsolete after the signature-validation vulnerability and must never be
  treated as sufficient. This design requires only `BatchV1_1`, whose ID is
  the SHA-512Half of its canonical name:
  `9F287AED3CDB50A7BD1ACEC24296A30C9B5230CCD136219317AC790E3B884377`.
  As of this design date, `BatchV1_1` is supported on `rippled` develop but is
  described by the public amendment registry as a future replacement, not an
  enabled mainnet amendment.
  <https://xrpl.org/resources/known-amendments>
  <https://github.com/XRPLF/rippled/blob/develop/include/xrpl/protocol/detail/features.macro>
- `xrpl-py` 5.0 contains the `Batch` model, Batch fee/autofill support,
  `encode_for_signing_batch`, and Batch signer helpers. LFG will require
  `xrpl-py>=5.0.0` rather than depend on an unpinned transitive environment.

## User experience

### Link discovery

An X post links to `https://build.letseffinggo.com/?action=mint`. X renders the
existing Open Graph card. Opening it lands directly on the mint action in the
PWA rather than on a generic home screen.

X does not provide a general third-party transaction renderer comparable to a
Solana Blink client. In v1, X is the discovery surface and the installed PWA /
Xaman pair is the execution surface. The action API is intentionally public and
standardized so a future X, wallet, explorer, or browser extension can render
the same action without LFG-specific code.

### One-approval mint

1. A returning web user is already associated with an XRPL address by the
   existing web session. A new user completes the existing Xaman SignIn first.
2. The action quotes LFGO or XRP exactly as the current `MintSession` does.
3. LFG reserves supply headroom and an edition number, composes the art, uploads
   the image/metadata, and prepares the Batch. The UI shows `Preparing your
   mint...`; no payment or mint has happened yet.
4. The UI opens one Xaman Batch request. The wallet must show the Batch mode and
   all three ordered inner transactions.
5. The buyer approves once. Xaman submits the buyer-signed outer transaction.
6. LFG verifies the outer and all three inner results from the validated ledger,
   resolves the `NFTokenID` from the mint metadata, records the mint, and shows
   the completed NFT. There is no second accept screen.

If the user rejects or lets the request expire, no payment, NFT, or offer lands.
Generated files are harmless unreferenced artifacts and the edition/headroom
reservation is released. The issuer Ticket is not reassigned until the outer
transaction's `LastLedgerSequence` has passed.

## XRPL Actions draft application standard

This is an application/interoperability XLS, not a ledger amendment. Its draft
will live in `docs/xls/xrpl-actions.md` and define the following v1 contract.

### Discovery document

`GET /.well-known/xrpl-actions.json`

```json
{
  "version": "1",
  "rules": [
    {
      "pathPattern": "/actions/**",
      "apiPath": "https://letseffinggo.tail82fcc6.ts.net/lfg/api/actions/**"
    }
  ]
}
```

The document maps shareable action URLs to API URLs without requiring a client
to scrape HTML. Because `build.letseffinggo.com` is currently a GitHub Pages
front end with a cross-origin API, the production Pages artifact supplies this
as the static file `/.well-known/xrpl-actions.json` and the deploy workflow
rewrites `apiPath` to the configured absolute API base. The aiohttp service also
serves the same document for same-origin and staging deployments.

### Action metadata

`GET /api/actions/mint`

```json
{
  "type": "xrpl-action",
  "version": "1",
  "chain": "xrpl:mainnet",
  "icon": "https://build.letseffinggo.com/assets/mascot.png",
  "title": "Mint an LFG",
  "description": "Pay, mint, and receive your NFT atomically.",
  "label": "Mint",
  "transactionTypes": ["Payment", "NFTokenMint", "NFTokenAcceptOffer"],
  "requirements": {
    "amendments": ["NFTokenMintOffer", "BatchV1_1"],
    "wallet": "xaman"
  },
  "enabled": true,
  "links": {
    "actions": [{"label": "Mint", "href": "/api/actions/mint"}]
  }
}
```

`enabled` is derived from the connected ledger, not configuration alone. On a
network without the required amendments it is `false` with a stable
`unavailableReason` code.

### Action creation and asynchronous preparation

`POST /api/actions/mint`

LFG requires the existing bearer session because preparing a mint consumes
composition, CDN, supply-headroom, and ticket resources. The JSON body is:

```json
{
  "account": "rBuyer...",
  "campaign": "x-mint-link"
}
```

`account` must exactly match the authenticated wallet. `campaign` is optional,
bounded, and copied only into the existing provenance memo allowlist. Art and
metadata preparation can outlive a normal interactive HTTP response, so the
creation call returns `202 Accepted` immediately:

```json
{
  "type": "xrpl-action-session",
  "version": "1",
  "sessionId": "...",
  "state": "preparing",
  "status": "/api/actions/mint/..."
}
```

The client polls the status URL. Once the session reaches
`awaiting_signature`, the status response is transport-neutral and also
contains Xaman convenience links:

```json
{
  "type": "xrpl-sign-request",
  "version": "1",
  "sessionId": "...",
  "account": "rBuyer...",
  "transaction": {"TransactionType": "Batch"},
  "wallets": {
    "xaman": {
      "uuid": "...",
      "deeplink": "https://xumm.app/sign/...",
      "qr": "https://..."
    }
  },
  "expiresAt": "2026-07-18T00:00:00Z"
}
```

The real `transaction` value is complete canonical XRPL JSON, including the
issuer `BatchSigner`, so another compatible wallet can sign and submit it. The
issuer signature authorizes only the mode and exact ordered inner transaction
hashes; publishing it does not authorize any other mint.

`GET /api/actions/mint/{session_id}` returns the current state. It carries the
sign request shown above when ready, and the validated outer hash, inner hashes,
NFT ID, and image URL when complete. This two-response pattern is part of the
draft so expensive action providers can prepare transactions asynchronously
without proprietary client behavior.

Stable error codes include `batch_unavailable`, `action_disabled`,
`wallet_mismatch`, `capacity_reached`, `ticket_unavailable`, `sequence_stale`,
`signing_unavailable`, `expired`, `rejected`, `batch_failed`, and
`outcome_indeterminate`.

## Exact Batch construction

Let:

- `U` be the buyer's next account sequence;
- `T` be a leased, existing issuer Ticket;
- `O` be the deterministic NFT sell-offer index derived from
  `(config.SIGNING_ACCOUNT, T)`;
- `P` be the current payment parameters from `MintSession._payment_params()`;
- `M` be the uploaded metadata URI.

The canonical structure is:

```json
{
  "TransactionType": "Batch",
  "Account": "rBuyer...",
  "Sequence": "U",
  "Flags": 65536,
  "LastLedgerSequence": "L",
  "SourceTag": 2606160021,
  "Memos": [],
  "BatchSigners": [
    {
      "BatchSigner": {
        "Account": "rIssuer...",
        "SigningPubKey": "...",
        "TxnSignature": "..."
      }
    }
  ],
  "RawTransactions": [
    {
      "RawTransaction": {
        "TransactionType": "Payment",
        "Account": "rBuyer...",
        "Destination": "P.destination",
        "Amount": "P.amount",
        "Sequence": "U+1",
        "Fee": "0",
        "SigningPubKey": "",
        "Flags": 1073741824,
        "SourceTag": 2606160021,
        "Memos": []
      }
    },
    {
      "RawTransaction": {
        "TransactionType": "NFTokenMint",
        "Account": "rIssuer...",
        "Sequence": 0,
        "TicketSequence": "T",
        "URI": "hex(M)",
        "NFTokenTaxon": 0,
        "TransferFee": 0,
        "Amount": "0",
        "Destination": "rBuyer...",
        "Fee": "0",
        "SigningPubKey": "",
        "Flags": "NFT_FLAGS | 1073741824",
        "SourceTag": 2606160021,
        "Memos": []
      }
    },
    {
      "RawTransaction": {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": "rBuyer...",
        "NFTokenSellOffer": "O",
        "Sequence": "U+2",
        "Fee": "0",
        "SigningPubKey": "",
        "Flags": 1073741824,
        "SourceTag": 2606160021,
        "Memos": []
      }
    }
  ]
}
```

The illustrative fields above are serialized with their actual XRPL types.
`TransferFee` is included only when the configured NFT is transferable, just as
in the current `mint_nft` helper. `NFTokenTaxon`, NFT flags, issuer override,
SourceTag, and provenance memos retain the existing production rules.

The outer buyer account is intentional:

- the buyer's normal outer signature authorizes both buyer inner transactions;
- only the issuer needs a `BatchSigner`, which LFG attaches before creating the
  Xaman request;
- Xaman can sign and submit the outer transaction in one approval;
- the buyer pays the small Batch network fee, consistent with submitting a
  wallet transaction;
- LFG never receives a user-signed standalone Payment that could be replayed
  outside the Batch.

`xrpl-py` Batch autofill is used with `signers_count=1`. It assigns the buyer's
outer and two consecutive inner sequences, calculates the aggregate Batch fee,
sets the outer `LastLedgerSequence`, and preserves the mint's explicit
`Sequence: 0` / `TicketSequence: T`.

### Regular-key issuer signing

Production uses `config.SIGNING_ACCOUNT` with `config.SEED`, which can be its
regular key rather than its master seed. `xrpl-py.sign_multiaccount_batch`
assumes `wallet.address` is the authorizing account and is therefore not safe
for this deployment shape.

LFG will implement a small signer that:

1. hashes the Batch mode and ordered inner transaction IDs with
   `encode_for_signing_batch`;
2. signs that hash using the seed-derived private key;
3. emits `BatchSigner.Account = config.SIGNING_ACCOUNT`, the seed-derived
   public key, and the signature;
4. refuses to sign unless the raw mint account is exactly
   `config.SIGNING_ACCOUNT` and the complete three-leg invariant passes.

`rippled` then validates the public key against the account's master or regular
key in the normal way.

## Deterministic offer ID

The helper `nft_offer_id(account, sequence_or_ticket)` implements the protocol
keylet:

```text
SHA512Half(
  uint16_be(0x0071) ||
  decode_classic_address(account) ||
  uint32_be(sequence_or_ticket)
)
```

`0x0071` is the `NftokenOffer` ledger namespace (`'q'`). The helper is tested
against independent `rippled`/known-ledger vectors, validates a classic address
and a uint32 ticket, and never derives from the seed wallet's address when a
regular key is in use.

## Ticket pool and replay safety

Issuer sequence numbers cannot safely remain fixed during an interactive Xaman
approval: unrelated issuer transactions can consume the sequence before the
user signs. The mint therefore uses a Ticket.

Add a durable SQLite ticket lease table keyed by `(network, account,
ticket_sequence)` with `session_id`, `state`, `leased_at`,
`last_ledger_sequence`, and optional `outer_tx_hash`. Allocation is atomic:
discover existing Ticket objects, subtract active leases, and lease one in a
single immediate transaction.

Tickets are managed as follows:

- a provisioning CLI creates issuer Tickets on the selected network; it is
  explicit and is never run as an import/startup side effect;
- a ticket remains leased while a Xaman request can still validate;
- success marks it consumed after the mint inner result validates;
- rejection/expiry releases it only after the Batch `LastLedgerSequence` is
  closed and a final outer-hash lookup confirms no validated transaction;
- an indeterminate submission quarantines the ticket until reconciliation;
- startup reconciliation compares leases with ledger Ticket objects and stored
  transaction hashes before making any ticket available.

This prevents an old signed Batch and a new Batch from racing for the same
ticket. The user sequences can still become stale if the user submits another
transaction while the request is open. That fails the Batch without payment or
mint; the UI offers a newly constructed request.

## Amendment and capability gate

The feature is dark by default behind `XRPL_ACTIONS_BATCH_ENABLED=false`.
Enabling the environment flag is necessary but not sufficient. The service
queries the connected `rippled` `feature` RPC and caches the result briefly.
The action is enabled only if all of the following hold:

- `NFTokenMintOffer` is supported and enabled;
- amendment ID
  `9F287AED3CDB50A7BD1ACEC24296A30C9B5230CCD136219317AC790E3B884377`
  (`BatchV1_1`) is supported and enabled;
- `xrpl-py>=5.0.0` Batch serialization is available;
- at least one unleased issuer Ticket is available;
- the existing network-seam checks pass.

The obsolete Batch ID is hard-denied even if returned by a server. No
configuration alias can substitute it for `BatchV1_1`.

When the gate is closed, the existing two-approval mint remains unchanged and
the action metadata reports `enabled: false`; the app never labels the legacy
flow atomic. Testnet/dev environments can exercise the path as soon as their
connected ledger enables `BatchV1_1`.

## Session lifecycle and persistence

The current flow cannot simply wrap its final transactions because it waits for
an already-validated Payment before composing and minting. Add an atomic action
path alongside it and refactor only the shareable preparation/settlement work.

States:

```text
PREPARING
  -> AWAITING_BATCH_SIGNATURE
  -> CONFIRMING
  -> DONE

PREPARING / AWAITING_BATCH_SIGNATURE / CONFIRMING
  -> REJECTED | EXPIRED | FAILED | INDETERMINATE
```

Persist enough information before publishing the Xaman request to recover after
a process restart: session/account/network, quote, edition number, metadata and
image URLs, ticket, offer ID, canonical Batch JSON, outer and inner hashes,
Xaman UUID, `LastLedgerSequence`, headroom claimant, and state timestamps.
Secrets and seed material are never persisted.

Preparation reuses current composition/upload logic but does not call
`mint_nft`, `create_nft_offer`, or `wait_for_payment`. Settlement reuses the
current database record, rarity, image archive, headroom, firehose, and XRP
buy-and-burn behavior only after validated Batch success.

For the XRP path, the first inner Payment sends XRP to the bot wallet. After
the complete Batch validates, LFG performs the existing best-effort LFGO
buy-and-burn with at most the collected XRP. That post-transaction economic
operation remains best-effort and is not part of the ownership atomicity
guarantee, matching current behavior.

## Validation and completion rules

The outer Batch returning `tesSUCCESS` does not prove its inner operations
succeeded. A session is `DONE` only when LFG verifies:

- the outer transaction is validated;
- all three known inner transaction hashes are validated in the same ledger;
- every inner result is exactly `tesSUCCESS`;
- each inner metadata points to the expected outer `ParentBatchID`;
- the Payment account, destination, amount, currency, and issuer match the
  frozen quote;
- the mint account, URI, taxon, flags, zero-price offer destination, SourceTag,
  and provenance memos match the prepared action;
- the accept account and `NFTokenSellOffer` match the buyer and precomputed
  offer ID;
- the mint metadata resolves one NFT ID and the accept metadata confirms the
  same token's transfer to the buyer.

Never infer success from a signed Xaman payload alone. Never blindly regenerate
or resubmit after an unknown transport outcome. Reconcile the fixed hashes until
validation or `LastLedgerSequence` finality makes the result definitive.

## Security invariants

- **Payment first:** array index 0 is always the exact current mint payment.
- **No double charge:** the mint-created offer is always amount zero; payment
  occurs only in the first leg.
- **Buyer locked:** mint `Destination` equals Payment `Account` and accept
  `Account`.
- **One NFT path:** accept references the deterministic offer created by the
  mint ticket; arbitrary offer IDs are rejected before issuer signing.
- **All or nothing only:** no other Batch mode can pass the signing guard.
- **Exact accounts:** the only inner accounts are the authenticated buyer and
  `config.SIGNING_ACCOUNT`.
- **Exact ordering/types:** precisely Payment, NFTokenMint,
  NFTokenAcceptOffer; no fourth transaction and no nesting.
- **SourceTag/memos:** outer and all three inner transactions carry source tag
  `2606160021` and the existing bounded provenance schema.
- **Wallet binding:** Xaman payload option `signer` equals the authenticated
  classic address. A different wallet cannot approve it.
- **Short validity:** Xaman expiry and outer `LastLedgerSequence` are bounded;
  an expired action is rebuilt, never extended in place.
- **Issuer least authority:** the backend signature covers only the mode and
  exact ordered inner hashes. The signing helper refuses generic Batch input.
- **No old Batch:** the obsolete amendment ID is never accepted.
- **Rate and capacity limits:** existing one-active-mint/headroom controls plus
  per-wallet/IP action-creation limits prevent unbounded composition, payload,
  and ticket exhaustion.

## Code shape

Expected implementation seams:

- `lfg_core/xrpl_actions.py`: action metadata/schema, capability checks,
  deterministic offer key, canonical Batch builder, invariant validator,
  regular-key-aware issuer Batch signer, and ledger reconciliation;
- `lfg_core/action_store.py`: durable action session and ticket lease storage;
- `lfg_core/mint_flow.py`: split reusable prepare-assets and post-validation
  settlement from the legacy payment-first state machine without changing the
  legacy path;
- `lfg_core/xumm_ops.py`: create an enforced-signer Batch payload and retain
  Xaman rejection/expiry/tx-hash status;
- `lfg_service/app.py`: service-side well-known discovery, action
  metadata/create/status, auth/rate-limit wiring, startup reconciliation, and
  static PWA route;
- `webapp/client/.well-known/xrpl-actions.json` and the Pages workflow: primary
  build-domain discovery document with the deployed absolute API base;
- `webapp/client/app.js` and styles: `?action=mint` boot path, preparation,
  single Batch approval, confirming, expiry/retry, and completion states;
- `scripts/provision_batch_tickets.py`: explicit network-checked ticket pool
  provisioning/status CLI;
- `docs/xls/xrpl-actions.md`: implementation-independent draft XLS;
- `requirements.txt`, environment examples, README, and ops documentation:
  dependency floor, dark-launch flags, ticket provisioning, and rollout.

## Testing

### Pure protocol tests

- Offer ID golden vectors and malformed address/uint32 cases.
- Exact Batch ordering, accounts, payment amounts for LFGO and XRP, zero-price
  destination offer, source tag, memos, inner fee/signing fields, flags,
  sequences, ticket, and calculated offer reference.
- Deserialize/serialize the complete Batch with `xrpl-py` 5.0 and verify the
  three inner hashes remain stable.
- Verify the backend Batch signature cryptographically and under both a master
  seed and a regular-key seed/address pairing.
- Mutation tests: changing amount, destination, URI, ticket, offer ID, order,
  mode, account, memo, or adding/removing a leg invalidates the issuer signature
  or the local signing guard.

### Capability and ticket tests

- Gate opens only for enabled `NFTokenMintOffer` + exact `BatchV1_1`.
- Unsupported, disabled, malformed, unreachable, and obsolete-Batch-only nodes
  fail closed.
- Concurrent ticket leases never duplicate; restart reconciliation handles
  available, consumed, expired, and quarantined tickets.
- Ticket release waits through `LastLedgerSequence`; an indeterminate result is
  never reused.

### Service/state tests

- Metadata/discovery schemas and disabled response.
- Authenticated account match, foreign account rejection, rate/capacity limits.
- Preparing -> Xaman payload -> confirming -> validated completion.
- Reject, expire, stale buyer sequence, Xaman outage, outer-only success,
  missing/mismatched inner metadata, validated inner failure, and indeterminate
  transport outcomes.
- Payment rollback behavior is represented by requiring all three inner
  results; no database mint or buy-and-burn occurs on any incomplete Batch.
- Restart resumes by fixed hashes without creating another Xaman payload or
  signing a second Batch.
- Existing legacy mint, bulk mint, marketplace, Discord, Telegram, and web
  suites remain unchanged when the feature flag is off.

### Browser/manual verification

- X action URL renders the intended card and opens the mint action route.
- Returning web user sees one Xaman request, whose review lists Payment first,
  NFTokenMint second, NFTokenAcceptOffer third, and `ALLORNOTHING`.
- Rejecting signs nothing; successful test-ledger approval yields one outer
  Batch and three same-ledger inner records linked by `ParentBatchID`, with the
  buyer owning the NFT.

## Rollout

1. Merge dark with `XRPL_ACTIONS_BATCH_ENABLED=false` and legacy mint untouched.
2. Provision a small issuer Ticket pool on a non-production network.
3. Enable `BatchV1_1` on the test ledger and run model, service, and live-ledger
   verification, including regular-key signing.
4. Publish the action discovery/metadata endpoints while reporting
   `enabled:false`; this allows clients to integrate before mainnet activation.
5. After the exact `BatchV1_1` amendment is enabled on mainnet, provision the
   production ticket pool, enable the flag for an allowlisted wallet cohort,
   and monitor signing, stale-sequence, Batch failure, ticket, and reconciliation
   metrics.
6. Expand the cohort, then make the X/PWA mint link default to the atomic action.

## Non-goals

- No claim that X itself executes an XRPL transaction inline in v1.
- No use of the obsolete `Batch` amendment.
- No new `NFTokenMintAndTransfer` ledger transaction and no additional ledger
  amendment unless testing disproves the verified create-then-accept Batch
  behavior.
- No removal of the current mint flow before `BatchV1_1` is enabled and the new
  path has completed a staged rollout.
- No batching of bulk mints; the three-leg action is for a single interactive
  mint.
- No automatic mainnet Ticket creation or feature activation during deploy.
