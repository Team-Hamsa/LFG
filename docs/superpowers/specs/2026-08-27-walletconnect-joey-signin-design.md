# WalletConnect / Joey Wallet sign-in, wallet linking and transaction signing — design

**Issue:** #447 (User Profiles epic #444, step 3). Supersedes the issue's original
"signin/link-only MVP" text — user decision 2026-08-27: a Joey session must be able
to *do* everything a Xaman session can.
**Status:** approved design, 2026-08-27. Builds on #433 (`lfg_core/signing/` seam),
#445 (`wallet_token_links` correlation) and PR #449 (vendored WalletConnect bundle).

## Decisions (locked)

| # | Decision |
|---|----------|
| 1 | **Provider: WalletConnect v2 / Joey Wallet only.** No xrpl-connect, Crossmark or GemWallet. The server side is provider-agnostic so other client adapters can be added later without server changes. |
| 2 | **Sign-in / link proof = a signed, never-submitted pseudo-transaction** (`AccountSet`, `Fee:"0"`, `Sequence:0`, `LastLedgerSequence:0`, nonce memo), verified locally with xrpl-py. Joey exposes no `xrpl_signMessage`. |
| 3 | **Explicit wallet linking** writes a new append-only `wallet_proof_links` table that becomes a third edge type in `identity._bucket_bfs`. **Unlinking is out of scope** (admin sqlite `DELETE`). |
| 4 | **Full transaction signing** over WalletConnect: Joey autofills + submits; the client posts the hash; the server verifies on-ledger and never trusts the client. |

## Joey / WalletConnect contract (verified 2026-08-27 against docs.joeywallet.xyz and `@joey-wallet/wc-core` 1.0.4)

- Chains: `xrpl:0` mainnet, `xrpl:1` testnet, `xrpl:2` devnet.
- Methods: `xrpl_signTransaction`, `xrpl_signTransactionFor`, `xrpl_signTransactionBatch`.
  Only `xrpl_signTransaction` is used.
- Request: `{ tx_json, options?: { autofill?: bool, submit?: bool } }`.
- Response: `{ tx_json }` — the signed transaction (`SigningPubKey`, `TxnSignature`),
  plus `hash` when submitted. A submit failure inside the wallet may return no hash.
- Vendored bundle `webapp/client/vendor/walletconnect.js` exports `SignClient` (2.21.1)
  and `WalletConnectModal` (2.7.0); not imported anywhere until this work.

## 1. Architecture — provider dispatch at the two chokepoints

The #433 seam (`BaseSigningProvider`, `get_provider`) exists but **no flow calls it**:
all ~19 payload-create sites and ~40 status-poll sites still call `xumm_ops.*`
directly (app.py, mint/swap/market/bulk/burn2mint flows). Every builder ends in
`xumm_ops._create_xumm_payload(...)` and every poll goes through
`xumm_ops.get_payload_status(uuid)`; every flow already threads a per-session
`push_user_token` into each builder. The design piggybacks on that plumbing:

- Each session dataclass (`MintSession`, `SwapSession`, market
  `List/Buy/Cancel/Bid/BidAccept/TraitSell` sessions, `BulkMintJob`, burn2mint) gains
  `provider: str = "xaman"` (additive; persisted records load unchanged). It is threaded
  through the same builder kwargs `push_user_token` uses, sourced from the session token.
- `_create_xumm_payload` and `get_payload_status` dispatch via `get_provider(name)`:
  - `xaman` → today's code, wrapped by `XamanProvider`; zero behavior change.
  - `walletconnect` → `WalletConnectProvider` (`lfg_core/signing/walletconnect.py`).
- `WalletConnectProvider._create()` stores the stamped txjson in a new `sign_requests`
  table (app DB, `lfg_core/signing/store.py`):
  `id TEXT PK ("wc-<uuid4>"), wallet, purpose ("tx"|"signin"|"link"), txjson JSON,
  nonce, state ("pending"|"signed"|"rejected"|"failed"|"mismatch"|"expired"|
  "cancelled"|"consumed"), txid, result_json, ip, created_at, expires_at`.
  It returns the handle dict flows already expect:
  `{uuid:"wc-…", sign_mode:"walletconnect", txjson, xumm_url:None, qr_url:None,
  pushed:False, push:None}`.
- Handle ids are namespaced (`wc-` prefix) so `get_payload_status` routes without a
  lookup table.
- The session token (`_issue_session_token` / `_session_from_request`) gains a
  `provider` field, set at sign-in; handlers pass it into session constructors exactly
  where they pass `push_user_token`.
- New env: `REOWN_PROJECT_ID` (feature OFF and button hidden when unset),
  `WC_SURFACES` (default `web,telegram`; see §4).

Not covered (Xaman-only by design): the Discord bot's trustline button and the CLI
economy scripts (no web session). Rewiring every flow onto `get_provider` directly is a
follow-up, not part of #447.

## 2. Sign-in and wallet-link proof

### Nonce issue
`POST /api/web/signin` accepts `{provider:"walletconnect"}` (default `xaman` → the
existing XUMM SignIn path, unchanged). WC branch: mint a 32-byte nonce, insert a
`sign_requests` row (`purpose:"signin"`, `expires_at = now + SIGNIN_TTL`, `ip`),
return `{sign_id, nonce, source_tag, expires_at}`. Same per-IP 5/60 s limiter.

### Proof transaction (built client-side, canonical shape enforced server-side)
```
{ TransactionType:"AccountSet", Account:<wallet>, Fee:"0", Sequence:0,
  LastLedgerSequence:0, SourceTag:2606160021,
  Memos:[ <lfg provenance memos: action=signin|link, platform=webapp>,
          { MemoType:hex("lfg/nonce"), MemoData:hex(nonce) } ] }
```
Signed via `xrpl_signTransaction` with `options:{autofill:false, submit:false}`.
`Fee:"0"` (`temBAD_FEE`) and `LastLedgerSequence:0` make it unsubmittable anywhere.

### Verify — `POST /api/web/signin/proof {sign_id, tx_json}`
`lfg_core/signing/proof.py`, pure xrpl-py, no network:
1. Row exists, `pending`, unexpired, `purpose` matches → else 410 / 409.
2. `TransactionType=="AccountSet"`, `Fee=="0"`, `Sequence==0`,
   `LastLedgerSequence==0`, `SourceTag==SOURCE_TAG`, nonce memo == stored nonce
   (exact bytes). **Any field outside the allowlist rejects** — a real transaction can
   never be smuggled in as a "proof".
3. `derive_classic_address(SigningPubKey) == Account`. RegularKey-signed proofs are
   NOT accepted in v1 (documented limitation).
4. `keypairs.is_valid_message(encode_for_signing(tx), TxnSignature, SigningPubKey)`.
5. Mark the row `consumed` (single use), then as today:
   `identity_store.link("web", wallet, wallet, wallet)`; issue a session token with
   `provider:"walletconnect"`. No `user_token` capture (Joey has none).

### Explicit wallet link — `POST /api/wallet/link` (start) + `/proof` (authed)
Same nonce + proof machinery with `purpose:"link"`; the proving wallet must differ from
the session wallet (same → 400 `same_wallet`). On success:
`INSERT OR IGNORE INTO wallet_proof_links(wallet_a, wallet_b, proof_kind, linked_at)`
with `(a, b)` ordered lexically, `proof_kind = "wc-signed-tx"`. No `wallet_links` row is
written — the graph edge comes only from `wallet_proof_links`, so it is auditable as a
distinct edge kind. `_bucket_bfs` gains a third neighbor query over that table (both
directions); linking a wallet already in another bucket merges the buckets.

A Xaman session links a second wallet by reusing the existing XUMM SignIn payload with
`purpose:"link"` — no new signing code, same table write with `proof_kind:"xaman-signin"`.

## 3. Transaction signing on the WalletConnect path

**Create.** Builders are called as today with `provider=session.provider`. For
`walletconnect` the stamped txjson (SourceTag + memos from `stamp_and_validate`,
`Account` pinned to the session wallet per the #314 signer-pinning rule) is stored as a
`wc-` row, `purpose:"tx"`, `expires_at = now + 900 s` (same lifetime as XUMM payloads).
Every session poll endpoint already returns the handle dict, so the client receives
`txjson` to sign.

**Sign + submit (client).** `xrpl_signTransaction {tx_json, options:{autofill:true,
submit:true}}` — Joey autofills `Fee`/`Sequence`/`LastLedgerSequence`, signs, submits.
Client then `POST /api/sign/{wc-id}/result` with `{hash}`, `{rejected:true}` (user
cancel) or `{error}` (wallet failure / hash-less response). Client-side timeout at
`expires_at` → `rejected`.

**Verify (server, never trusts the client).** `POST /api/sign/{id}/result`, authed,
row wallet must equal session wallet (else 403 `not_your_request`):
- `rejected` → `state=rejected`; `error` → `state=failed`.
- `hash` → `xrpl_ops.get_tx(hash)`. Require: found, `validated==true`,
  `tx.Account == row.wallet`, `TransactionType` matches, and a **semantic match** of the
  stored txjson minus autofill fields (`Fee`, `Sequence`, `LastLedgerSequence`,
  `SigningPubKey`, `TxnSignature`, `hash`, unset `Flags`, `NetworkID`). Mismatch →
  `state=mismatch`, WARNING log, 409 `tx_mismatch`; the flow fails the way
  `signer_mismatch` does today. Match → `state=signed`, `txid=hash`.
- Found but not yet validated → 202 `tx_not_found`, client retries; row stays `pending`.
  Not found after `expires_at` → `expired` (410).

**Status.** `get_payload_status("wc-…")` maps the row to the dict/`SignStatus` shape
flows read today: `signed` (True / False / None-pending), `resolved`, `txid`,
`signer=wallet`, `user_token=None`, `dispatched=(state=="signed")`. Flows' existing
"validated + tesSUCCESS" checks (market fetch-by-hash, mint payment watch) run unchanged.

**Cancel.** `cancel()` sets `state=cancelled`. The #260 open-payload cap script skips
`wc-` rows; expiry is the row's `expires_at`.

### Cross-wallet payloads always go through Xaman (gap closed 2026-08-28)

A signed transaction is only valid when `SigningPubKey` is the `Account`'s own master
or regular key, or a member of that account's SignerList
([transaction common fields](https://xrpl.org/docs/references/protocol/transactions/common-fields)).
A WalletConnect session is bound to exactly one connected account, so a `wc-` request
whose `Account` is *another* wallet can never be signed from it (Joey's
`xrpl_signTransactionFor` is the multi-sign variant — it needs the target account to
list the connected account as a signer, which LFG users never have).

The app already builds one such payload: the #446 linked-wallet trustline
(`POST /api/brix/trustline {wallet: <bucket sibling>}`), whose `TrustSet.Account` is the
sibling, not the session wallet. Rule, enforced at the chokepoint rather than per call
site: **`_create_xumm_payload` forces `provider="xaman"` whenever
`txjson.Account != wallet` (the session wallet)**, logging the downgrade at INFO. The
XUMM QR / deep link is signable by whichever Xaman install holds that account, and the
existing signer-mismatch guard on the trustline status poll (#441) still rejects a
wrong-wallet signature. The handle then carries `sign_mode:"xaman"` even inside a Joey
session, and the client keys its rendering on `sign_mode` per §4 — so the claim-all
"Set trustline" row shows the QR with copy "Scan with the Xaman app holding
`<wallet>`", never the "Approve in Joey Wallet…" spinner. A future "connect that
wallet in Joey instead" switch is out of scope (v2). Any new flow that hands a payload
to a different wallet inherits this rule automatically.

**Known gaps (documented, not solved):** #58 pre-simulate does not run on the Joey path
(Joey autofills; the final tx is never seen before submit). RegularKey-signed accounts
work for transactions (verified by `Account` on-ledger) but not for the pseudo-tx proof.
`xrpl_signTransactionBatch` unused.

## 4. Client integration, error handling, surfaces

**Module.** `webapp/client/wc.js` (ES module, `?v=` cache-busted like `mint_pure.js`),
which lazily `await import('./vendor/walletconnect.js')` only on "Connect with Joey" —
Xaman users never load the bundle. `/api/config` gains
`walletconnect: {project_id, chain}` with `chain = xrpl:0|xrpl:1` from `XRPL_NETWORK`.

**Session lifecycle.** `SignClient.init({projectId, metadata})` →
`connect({requiredNamespaces:{xrpl:{chains:[chain], methods:['xrpl_signTransaction'],
events:[]}}})` → `WalletConnectModal.openModal({uri})` (desktop QR; mobile via the
modal's deep-link list — Joey mobile deep-linking is unverified, smoke item). On
approval: `wallet = session.namespaces.xrpl.accounts[0].split(':')[2]` → signin proof
(§2) → store the LFG session token in `localStorage` as today plus `lfg_wc_topic`.
Reload restores via `signClient.session.get(topic)`; `session_delete`/expiry clears both
and renders signed-out. All requests: `request({topic, chainId: chain, request:{method,
params}})`.

**UI states** (`renderSignin`/`applySignDelivery` keyed on `sign_mode`): no QR for
`walletconnect`; "Approve in Joey Wallet…" spinner, request fires immediately; rejection
→ "Cancelled in wallet — retry"; `202` → "Submitted, waiting for the ledger…" polling
`/result` every 3 s until `expires_at`; `mismatch` → hard error card. Profile page:
"Link another wallet" (§2) offering Joey or Xaman.

**Error taxonomy** (`code`, HTTP): `wc_disabled` 503, `bad_proof` 400, `proof_expired`
410, `proof_replayed` 409, `same_wallet` 400, `tx_not_found` 202/410, `tx_mismatch` 409,
`not_your_request` 403. `bad_proof` logs which check failed, never signature bytes.

**Surfaces.** Web (build.letseffinggo.com) and Telegram Mini App: on. Discord Activity
needs URL Mappings for `relay.walletconnect.com` (wss) and `api.web3modal.org`; the
button stays hidden there (`WC_SURFACES` default `web,telegram`) until the smoke test
confirms them.

## 5. Testing

- `tests/test_signing_proof.py`: build a proof with an xrpl-py `Wallet.create()` keypair
  and `sign()`; assert accept; mutate each field (wrong nonce, `Fee≠0`, extra
  `Destination`, pubkey≠account, tampered signature, reuse, expired) → each rejects with
  the right code.
- `tests/test_wc_provider.py`: chokepoint dispatch — builders with
  `provider="walletconnect"` return `wc-` handles; `get_payload_status` state mapping;
  `/result` with mocked `get_tx` (match, mismatch, unvalidated, wrong `Account`,
  foreign session).
- `tests/test_identity_proof_links.py`: `wallet_proof_links` edge merges buckets;
  fail-closed `BucketLookupError` behavior unchanged.
- Existing suites must pass untouched (`provider="xaman"` default = today's path).
- Smoke: staging (testnet, `xrpl:1`) with the real Joey app — sign-in, link a second
  wallet, one mint payment, one market list + cancel; then prod (`xrpl:0`).

## Ops

- Set `REOWN_PROJECT_ID` in both `.env`s (user holds it; never in chat/repo).
- Reown dashboard allowlist already has `build.letseffinggo.com`; add the Telegram
  origin if it differs.
- Discord Activity URL Mappings (relay + web3modal) before enabling `discord-activity`
  in `WC_SURFACES`.
