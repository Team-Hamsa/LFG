# Draft XLS: XRPL Actions — discoverable transaction links

| Field | Value |
| --- | --- |
| Status | Draft for discussion |
| Category | Application / interoperability standard |
| Version | 1 |
| Date | 2026-07-18 |
| Reference implementation | [Team-Hamsa/LFG](https://github.com/Team-Hamsa/LFG) |

## Abstract

XRPL Actions define a wallet-neutral way for a URL to describe an XRP Ledger
operation, prepare canonical transaction JSON, and hand that transaction to a
compatible wallet for review, signing, and submission. A discovery document
maps public action URLs to APIs. The API publishes human-readable metadata and
uses an asynchronous session when transaction preparation is expensive.

This proposal is an application standard. It does not add a transaction type,
change consensus, activate an amendment, or grant an action provider signing
authority over a user account. An action response contains normal XRPL JSON;
the wallet remains the final policy and signing boundary.

The motivating reference action is a payment-first NFT mint:

```text
Payment -> NFTokenMint + destination-locked free offer -> NFTokenAcceptOffer
                       one ALLORNOTHING Batch
```

The buyer approves once. Either all three ordered effects commit, or none do.

## Motivation

URLs are the Web's universal discovery primitive, while XRPL transactions are
usually initiated inside a particular application. A shared post, QR code,
message, or website link cannot currently tell a generic XRPL client:

- what operation the link represents;
- which network and amendments it requires;
- where to obtain canonical transaction JSON;
- whether preparation is still running;
- which account is expected to sign; or
- where to verify the result.

XRPL Actions supply that missing application-layer contract. They are similar
in product intent to “transaction links” on other chains, but use native XRPL
transaction JSON and XRPL's existing signing, sequencing, fee, amendment, and
finality rules.

## Scope and non-goals

Version 1 specifies discovery, metadata, preparation sessions, sign requests,
wallet behavior, errors, and security requirements.

It does not:

- make HTML or social-media previews trusted transaction descriptions;
- permit an action provider to sign for a user;
- define a new URI scheme;
- replace reliable transaction submission or validated-ledger verification;
- require `Batch` for simple single-transaction actions; or
- treat the obsolete `Batch` amendment as usable.

## Terminology

**Action URL**
: A public HTTPS URL shared with a person or client.

**Action provider**
: The HTTPS service that publishes metadata and prepares transaction JSON.

**Action client**
: A wallet, browser, explorer, PWA, or extension that resolves and renders an
  action.

**Sign request**
: Canonical XRPL transaction JSON plus the account expected to sign it.

**Preparation session**
: A server-side resource used when a transaction cannot be prepared within one
  request because it needs quoting, media generation, inventory reservation,
  additional-party signatures, or other bounded work.

## Versioning and transport

- All version 1 endpoints use HTTPS.
- JSON responses use `application/json` and UTF-8.
- Every document has the string field `version: "1"`.
- Unknown JSON fields must be ignored by version 1 clients.
- A client must reject a version it does not support.
- Redirects must not downgrade HTTPS. A client should limit redirects and
  reapply the same SSRF policy used for any other untrusted URL fetch.

## Discovery

An action-aware origin publishes:

```http
GET /.well-known/xrpl-actions.json
```

Example:

```json
{
  "version": "1",
  "rules": [
    {
      "pathPattern": "/actions/**",
      "apiPath": "https://api.example.com/api/actions/**"
    }
  ]
}
```

`rules` is ordered. The first matching `pathPattern` is selected. Version 1
supports a single terminal `**` wildcard. The substring matched by `**` is
substituted for `**` in `apiPath`. Without `**`, the rule is an exact path
match. Query strings and fragments are not included in matching or
substitution.

`apiPath` may be an absolute HTTPS URL or a same-origin absolute path. An
action client must reject other schemes and protocol-relative URLs.

For example, `/actions/mint` resolves through the example rule to
`https://api.example.com/api/actions/mint`.

Discovery is data, not authority. The client must still validate every field
returned by the action API and every field in the transaction.

## Action metadata

The resolved API URL supports `GET`. A successful response has this shape:

```json
{
  "type": "xrpl-action",
  "version": "1",
  "chain": "xrpl:mainnet",
  "icon": "https://example.com/icon.png",
  "title": "Mint an NFT",
  "description": "Pay, mint, and receive the NFT atomically.",
  "label": "Mint",
  "transactionTypes": [
    "Payment",
    "NFTokenMint",
    "NFTokenAcceptOffer"
  ],
  "requirements": {
    "amendments": ["NFTokenMintOffer", "BatchV1_1"]
  },
  "enabled": true,
  "links": {
    "actions": [
      {"label": "Mint", "href": "/api/actions/mint"}
    ]
  }
}
```

Required fields are `type`, `version`, `chain`, `title`, `description`,
`label`, `transactionTypes`, and `enabled`.

`chain` uses `xrpl:mainnet`, `xrpl:testnet`, `xrpl:devnet`, or an explicitly
documented private-network identifier. A client must show the network and must
not silently substitute another network.

`enabled` describes the provider's current readiness. If false, the response
should include a stable `unavailableReason`; clients must not call the create
operation while it is false.

`requirements.amendments` is informational for rendering, but the provider
and client must independently query the connected network when amendment
support affects validity.

Human-readable metadata is untrusted. A client must render it as text, bound
its length, proxy or safely fetch remote media, and never interpret it as HTML.

## Creating an action

The same resolved URL supports `POST`. The request is action-specific, but an
account-bound action should accept:

```json
{
  "account": "rBuyer..."
}
```

If the provider authenticated a wallet before creation, `account` must exactly
match that authenticated classic address. An arbitrary request-body account
must never override the authenticated account.

Simple actions may return a sign request directly with status `200`. Providers
that need bounded preparation should return `202 Accepted`:

```json
{
  "type": "xrpl-action-session",
  "version": "1",
  "sessionId": "4dd57e...",
  "state": "preparing",
  "status": "/api/actions/mint/4dd57e..."
}
```

The `status` URL must be HTTPS or a same-origin absolute path. Session
identifiers must be unguessable. An authenticated provider must bind the
session to the authenticated user, platform, and wallet, and return `404`—not
the foreign session's existence or state—to another principal.

## Session states

Version 1 defines these interoperable states:

| State | Terminal | Meaning |
| --- | --- | --- |
| `preparing` | No | Provider is preparing a fixed transaction. |
| `awaiting_signature` | No | A canonical sign request is available. |
| `confirming` | No | A wallet submitted it; validated outcome is pending. |
| `done` | Yes | Provider verified the required validated-ledger outcome. |
| `rejected` | Yes | User or wallet rejected the request. |
| `expired` | Yes | The request passed its signing or ledger validity bound. |
| `failed` | Yes | A definitive provider, wallet, or ledger failure occurred. |
| `indeterminate` | Yes | Safety requires operator reconciliation; do not retry blindly. |

Applications may add states, but generic clients must treat unknown states as
non-terminal and poll with bounded backoff until the advertised expiry.

## Sign request

When the session is ready, the status resource returns:

```json
{
  "type": "xrpl-sign-request",
  "version": "1",
  "sessionId": "4dd57e...",
  "state": "awaiting_signature",
  "account": "rBuyer...",
  "transaction": {
    "TransactionType": "Batch",
    "Account": "rBuyer..."
  },
  "wallets": {
    "xaman": {
      "uuid": "...",
      "deeplink": "https://xumm.app/sign/...",
      "qr": "https://...",
      "push": "sent"
    }
  }
}
```

`transaction` is complete canonical XRPL JSON, not a template. It contains any
provider or co-signer signatures already required. Wallet-specific links are
optional conveniences; another compatible wallet may import the canonical
transaction after applying its own policy.

The action client must verify at least:

- `transaction.Account == account` for the expected outer signer;
- the network and `NetworkID`, when present;
- `LastLedgerSequence` or another bounded validity rule;
- all transaction types, accounts, amounts, destinations, flags, memos, tags,
  signer entries, and inner transaction order;
- that the displayed summary is derived from transaction JSON, not metadata;
  and
- that no transaction field changed between review and signing.

## Completion response

A completed session returns identifiers sufficient for independent lookup:

```json
{
  "type": "xrpl-action-session",
  "version": "1",
  "sessionId": "4dd57e...",
  "state": "done",
  "outer_hash": "A1...",
  "inner_hashes": ["B2...", "C3...", "D4..."],
  "ledger_index": 123456,
  "nft_id": "0008..."
}
```

An action provider must not infer `done` from a wallet's “signed” response or a
submission response. It must use validated ledger results and validate the
action-specific postconditions.

## Error format

Non-2xx responses and terminal failure sessions should include a stable code:

```json
{
  "code": "ticket_unavailable"
}
```

Recommended version 1 codes include:

- `invalid_request`
- `unauthorized`
- `wallet_mismatch`
- `rate_limited`
- `action_disabled`
- `batch_unavailable`
- `mint_offer_unavailable`
- `ticket_unavailable`
- `capacity_reached`
- `storage_unavailable`
- `signing_unavailable`
- `rejected`
- `expired`
- `batch_failed`
- `outcome_indeterminate`

Error text is provider-controlled and untrusted. Clients should map known codes
to local copy and use a safe generic message for unknown codes.

## Authentication, CORS, and abuse control

Metadata and readiness should usually be public. Creation may require
authentication when it consumes scarce provider resources.

Cross-origin providers must return an explicit origin allowlist; they must not
combine credentialed requests with `Access-Control-Allow-Origin: *`. Standard
CSRF protections remain necessary for cookie-authenticated providers.

Providers should enforce:

- per-wallet and per-principal creation limits;
- one active equivalent action per wallet where duplicates are unsafe;
- bounded media, quote, inventory, signer, and Ticket reservations;
- idempotency or explicit conflict responses;
- request and response size limits; and
- retention and cleanup policies that preserve ledger reconciliation records.

## Batch actions

An action that uses `Batch` must follow the Batch amendment's own rules. In
particular, inner transactions are not separately signed transactions; their
authorization is committed through the outer Batch and `BatchSigners`.

Providers must require the exact amendment they built against. A similarly
named, obsolete, unsupported, or disabled amendment is not a substitute.

At the date of this draft, `rippled`'s `develop` feature registry marks
`BatchV1_1` as supported and default-no, while the original `Batch` amendment
is obsolete following a disclosed signature-validation flaw. Deployment must
query the connected server and validated ledger rather than relying on this
document's date-sensitive status.

The corrected amendment identifier used by the reference implementation is:

```text
BatchV1_1
9F287AED3CDB50A7BD1ACEC24296A30C9B5230CCD136219317AC790E3B884377
```

The obsolete identifier that must not satisfy this requirement is:

```text
Batch
894646DD5284E97DECFE6674A6D6152686791C4A95F8C132CCA9BAF9E5812FB6
```

Relevant primary references:

- <https://github.com/XRPLF/rippled/blob/develop/include/xrpl/protocol/detail/features.macro>
- <https://xrpl.org/resources/known-amendments>
- <https://xrpl.org/docs/concepts/transactions/batch-transactions>
- <https://xrpl.org/blog/2026/vulnerabilitydisclosurereport-bug-feb2026>

## Reference action: payment-first atomic NFT mint

The LFG reference action requires both `BatchV1_1` and `NFTokenMintOffer`.
`NFTokenMintOffer` lets `NFTokenMint` create a sell offer in the mint
transaction. It does not transfer the token by itself. See
<https://xls.xrpl.org/xls/XLS-0052-NFTokenMintOffer.html>.

Let `U` be the buyer's next sequence, `T` a durably leased issuer Ticket, `O`
the deterministic NFT offer index for the issuer and `T`, `P` the frozen mint
payment, and `M` the metadata URI.

The transaction is structurally:

```json
{
  "TransactionType": "Batch",
  "Account": "rBuyer...",
  "Sequence": 100,
  "Flags": 65536,
  "LastLedgerSequence": 123456,
  "RawTransactions": [
    {
      "RawTransaction": {
        "TransactionType": "Payment",
        "Account": "rBuyer...",
        "Destination": "P.destination",
        "Amount": "P.amount",
        "Sequence": 101,
        "Fee": "0",
        "SigningPubKey": "",
        "Flags": 1073741824
      }
    },
    {
      "RawTransaction": {
        "TransactionType": "NFTokenMint",
        "Account": "rIssuer...",
        "Sequence": 0,
        "TicketSequence": 9001,
        "URI": "hex(M)",
        "Amount": "0",
        "Destination": "rBuyer...",
        "Fee": "0",
        "SigningPubKey": "",
        "Flags": 1073741849
      }
    },
    {
      "RawTransaction": {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": "rBuyer...",
        "NFTokenSellOffer": "O",
        "Sequence": 102,
        "Fee": "0",
        "SigningPubKey": "",
        "Flags": 1073741824
      }
    }
  ],
  "BatchSigners": [
    {
      "BatchSigner": {
        "Account": "rIssuer...",
        "SigningPubKey": "...",
        "TxnSignature": "..."
      }
    }
  ]
}
```

The exact invariants are:

1. The outer mode is `tfAllOrNothing` (`65536`).
2. Inner order is `Payment`, `NFTokenMint`, `NFTokenAcceptOffer`.
3. The payment is first and matches the frozen quote exactly.
4. The mint uses `Sequence: 0` and a previously created, durably leased issuer
   `TicketSequence`.
5. The mint creates a sell offer with `Amount: "0"` and `Destination` equal to
   the authenticated buyer.
6. The accept references the deterministic offer created by that mint Ticket.
7. Buyer inner sequences follow the outer sequence (`U+1`, `U+2`).
8. Every inner transaction has fee zero, empty `SigningPubKey`, no individual
   signature, and `tfInnerBatchTxn` (`1073741824`). NFT flags are ORed onto the
   mint's inner flag.
9. The buyer signs the outer Batch once. The issuer's `BatchSigner` authorizes
   only the mode and exact ordered inner hashes.
10. The provider verifies the outer and all three fixed inner hashes as
    `tesSUCCESS` in one ledger, with the expected `ParentBatchID`, offer, and
    minted NFT ID, before reporting success.

Payment cannot land without the later mint and accept. A buyer cannot mint and
decline the transfer, because the mint and accept are in the same atomic unit.
The free destination-locked offer is a transfer mechanism, not a second price.

The offer index is known before signing:

```text
SHA512Half(
  uint16_be(0x0071) ||
  decode_classic_address(issuer_account) ||
  uint32_be(TicketSequence)
)
```

## Ticket safety

Interactive multi-account actions should not hold an issuer's normal account
sequence open. The reference action leases an existing issuer Ticket in a
durable store before publishing the sign request.

A Ticket must not be reused merely because the wallet rejected, expired, or
stopped reporting a request. The signed transaction may still be in flight.
The provider waits until `LastLedgerSequence` is closed, checks fixed hashes
and validated Ticket objects, then releases only a Ticket proven unused.
Unknown outcomes quarantine the Ticket for operator review.

Creating Tickets changes the issuer's owner count and reserve requirement.
Provisioning must therefore be an explicit, network-checked operator action,
not an application startup side effect.

## Replay and expiry

- Sign requests must have a bounded `LastLedgerSequence`.
- Session status must retain the exact prepared transaction and hashes.
- Expired transactions are rebuilt as new sessions; they are never extended or
  mutated in place after a co-signer signature exists.
- Providers must never blind-resubmit or regenerate after an ambiguous
  transport error.
- Clients should reject a transaction too close to expiry to review safely.
- A user sequence becoming stale causes the whole Batch to fail; no inner
  payment is treated as independently successful.

## Privacy

Discovery and metadata are public. Providers should not place user identifiers,
free-form user text, access tokens, seeds, or private session data in URLs,
metadata, XRPL memos, or public status resources. Authenticated status resources
must enforce ownership on every request.

## Backward compatibility

This proposal is additive. Existing applications and wallets continue to use
normal XRPL transaction submission. A provider may publish disabled metadata
before a required amendment activates. If an action is unavailable, a product
may offer a clearly labeled legacy flow, but it must not describe a sequence of
independent transactions as atomic.

## Reference implementation notes

The LFG implementation uses:

- `/.well-known/xrpl-actions.json` discovery;
- `GET/POST /api/actions/mint` plus authenticated status and active-session
  endpoints;
- exact amendment-ID gating with the obsolete Batch ID hard-denied;
- canonical transaction generation with `xrpl-py>=5.0.0`;
- regular-key-aware issuer Batch signing;
- SQLite session and Ticket leases persisted before Xaman payload creation;
- one Xaman enforced-signer request; and
- validated outer/inner-hash reconciliation before NFT settlement.

The environment flag is dark by default. It can close the feature but cannot
force it open when the connected ledger lacks the required amendments.

## Open questions

1. Should a future version standardize JSON Schema documents or keep the core
   small and use examples plus required fields?
2. Should private XRPL networks use CAIP-2 identifiers instead of the compact
   `xrpl:*` names used here?
3. Should wallet capability negotiation be a standard request field?
4. Should direct `200` sign requests and asynchronous `202` sessions remain in
   one version, or should all providers expose sessions for consistency?
5. Which independent wallet or explorer should serve as the second reference
   action client?
