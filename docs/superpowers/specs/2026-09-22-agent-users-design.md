# Agent users: `agent` sign-in provider, RegularKey proofs, live trait supply

Status: design approved in brainstorming 2026-09-22 and revised after an
adversarial review; not yet implemented. **Sequencing:** the operator decided
this PR waits for the first agent's feasibility verdict. That agent is the fly
(`joshuahamsa/lfg-fly`, `docs/specs/2026-09-22-fly-design.md` §3.0, "Phase
0"). That project is a separate repo and acts on LFG only through its public
API; this spec covers the LFG side.

## Why

A bot that holds its own wallet key should be able to use LFG exactly like a
person does. It should sign in, earn the BRIX drip, show up on leaderboards,
build, swap and trade on the same terms, with no special privileges. Today that
is almost possible through the Joey/WalletConnect path, where the client signs
and submits its own transactions and the server verifies them on-ledger
(#447). Three things are missing:

1. **A sign-in provider that isn't Joey.** The WalletConnect arm is gated on
   `REOWN_PROJECT_ID` (Joey's dashboard config), which has nothing to do with
   bots. And a bot's transactions are indistinguishable on-chain from a
   human's.
2. **RegularKey proofs.** `signing/proof.verify_proof` requires the proof to be
   signed by the account's master key. A bot operator should keep the master
   key in a phone wallet and give the bot only a RegularKey. Sign requests
   already accept a RegularKey (`_verify_request_tx` ignores `SigningPubKey`;
   the transaction validated under the account's own authority), but
   sign-in doesn't. Worse, revoking a key today would not end the sessions it
   already created.
3. **Live trait supply.** No public endpoint exposes per-trait live counts.
   `/api/rarity` returns *mint odds*, and the `nft_rarity` board returns only
   per-NFT scores.

The operator's decisions (2026-09-22): agents are **ordinary users**. They are
eligible for every leaderboard, the BRIX drip and SourceTag tracking, with
**no exclusions anywhere**. That includes funder dedupe in `unique_actors`: an
agent funded by its operator collapses into the operator's actor, as the #461
rule intends. The only visible difference is an honest provenance label:
their memos say `platform=agent`.

## 1. The `agent` sign-in provider

The web sign-in flow gains a third arm beside `xaman` and `walletconnect`:

- **`POST /api/web/signin {"provider": "agent"}`** is the same nonce + proof
  flow as the `walletconnect` arm (`handle_web_signin_start`). It creates a
  `sign_requests` row with purpose `signin` and returns the proof template.
  - `sign_requests` gains a self-migrating nullable **`provider`** column
    (NULL means `walletconnect`, which covers rows pending across the
    deploy).
  - `signing_proof.build_proof_tx` and `verify_proof` gain a `platform`
    parameter (today both hard-code `PLATFORM_WEBAPP`). The agent arm uses
    `PLATFORM_AGENT`.
  - The start response is the WalletConnect arm's shape
    (`sign_id, nonce, memos, source_tag`), plus `provider`.
- **`POST /api/web/signin/proof {sign_id, tx_json}`** is the unchanged
  endpoint. `_redeem_proof` reads the row's provider and verifies the proof
  against that provider's canonical memo block. The memos must still match
  *exactly*, so an agent row refuses a `webapp` proof and vice versa. On
  success it calls `_finish_web_signin(wallet, "agent", key_info)`, which
  issues an ordinary `platform="web"` session whose token carries
  `provider: "agent"`. The identity row is the same `("web", wallet)` row a
  Xaman web sign-in would create.
- **Gate:** `AGENT_SIGNIN_ENABLED` (env, default `0`) replaces
  `wc_enabled()` for this arm only. The flag ships dark and is turned on per
  stack. It shares the web sign-in IP rate limiters (`_web_rate_limited`,
  `_web_proof_rate_limited`).
- **Privileges:** none. The flag is self-declared, and anyone's bot, or a
  human, may use it. Its only effects are the memo label and that every
  transaction becomes a client-signed sign request.

### Client-signed dispatch

- `xumm_ops.should_use_walletconnect` becomes `should_use_client_signing`. It
  fires when `context.current_provider()` is in a new
  `CLIENT_SIGNED_PROVIDERS = frozenset({"walletconnect", "agent"})`. The
  cross-wallet rule is unchanged: `txjson.Account` must equal the session
  wallet, and never on a `SignIn`.
- Every other `current_provider() == "walletconnect"` check switches to the
  set membership test. The two Closet Market handlers that refuse
  WalletConnect (`handle_closet_bid_create`, `handle_closet_ask_buy`, via
  `_wallet_unsupported_response`) refuse **every** client-signed provider.
  Opening them is future work: it needs the sign-request field allowlist to
  cover TokenEscrow and `InvoiceID` Payments.
- **Discovery contract.** A bot finds its sign-request id in the flow response
  that carries the payload:
  - the `uuid` field where one exists (e.g. the trustline flow);
  - otherwise, by stripping `lfg-wc://` from the flow's payload-link field
    (`payment_link`, `accept_deeplink`, `accept`, `link`).

  The `lfg-wc://<sign_requests.id>` scheme and this extraction are a **public
  contract**. There is a test per flow the first agent uses (mint and bulk-mint
  payment, pending-offer accept, `POST /api/closet` accept, harvest delivery
  accept, BRIX trustline): each asserts that a client-signed session's link
  field is `lfg-wc://<id>` for an existing row owned by the session wallet.
  The bot then uses the existing `GET /api/sign/{id}` and
  `POST /api/sign/{id}/result`. On-ledger verification is unchanged.
- **Sweep:** no change. `_start_settlement_sweep` always starts the loop and
  `sweep_sign_requests` runs on every pass, whatever the flags. There's a test
  that an agent sign request past its TTL gets expired.

### The `agent` memo platform

- `memos.PLATFORM_AGENT = "agent"` joins the closed `_PLATFORMS` enum.
- **User-signed payloads.** Today about 30 call sites pre-build `memos_json`
  with `memos.platform_for_surface(x.platform)` before `_create_xumm_payload`
  runs:
  - `lfg_service/app.py` 13;
  - `swap_flow` 6, `mint_flow` 5;
  - `bulk_mint_flow` 2, `shop_flow` 2;
  - `market_flow` 1, `burn2mint_flow` 1.

  Each becomes `memos.platform_for_session(x.platform, x.provider)`, which
  returns `PLATFORM_AGENT` when the provider is `agent` and otherwise
  `platform_for_surface(x.platform)`.
- **Backend-signed operations started by a session.** These take no platform
  today and stamp `platform=backend` on every surface:
  - economy character modifies and Closet re-syncs, through
    `scripts/_economy_deps.build_economy_deps` → `xrpl_ops.modify_nft`;
  - economy accept payloads (`_accept_or_skip`);
  - BRIX claim payouts (`xrpl_ops.send_brix_claim`, hard-coded).

  A `platform` argument is threaded through `webapp/economy_api.start_*` →
  `_schedule` → `build_economy_deps(conn, …, platform=)` → the
  `char_modify_fn`, `_closet_modify`, `char_mint_fn` and `char_burn_fn`
  lambdas → `xrpl_ops.modify_nft(platform=)`. Separately, it goes from
  `handle_brix_claim`'s session through `_claim_one_wallet` →
  `send_brix_claim(platform=)`. The value is
  `memos.backend_platform_for(provider)`: `PLATFORM_AGENT` for an agent
  session and `PLATFORM_BACKEND` otherwise. **Human equips, harvests and
  claims keep today's `backend` label.** `action` values are unchanged
  (`modify` stays `modify`).
- **Never relabelled**, because they aren't session-initiated or their
  recovery matches memos exactly:
  - the sponsored-mint burn (its recovery compares the full decoded memo set,
    `xrpl_ops.py` ~2319);
  - buy-and-burn;
  - fee-cover refunds;
  - Closet Market backend transactions.
- **Persistence.** Session objects (mint, swap, economy, market, bulk-mint
  job records) persist the provider next to the platform, so resumed jobs and
  sweeps keep labelling an agent's transactions `agent`.
- `sourcetag_metrics.validate_payload` is unaffected: platform breakdowns are
  not part of the published payload.

## 2. RegularKey-aware ownership proofs

### Verification order

`_redeem_proof` reads `Account` from `tx_json` and first does one
`account_info` on the validated ledger, through the async JSON-RPC failover
pool (`xrpl_ops.async_rpc_client`). The lookup has three outcomes: found,
not found, or error. It then calls `verify_proof(…, authority=KeyAuthority(
regular_key, master_disabled, lookup_ok))`. **`verify_proof` stays pure and
remains the single gate.** Both steps complete **before `on_verified`
runs**, so a proof that fails the key check never writes a
`wallet_proof_links` edge.

| Signing key | Lookup succeeded | Lookup failed |
|---|---|---|
| derives to `Account` (master) | accept unless `lsfDisableMaster` is set → `master_disabled` | **accept** (today's behaviour; a deliberate availability trade-off) |
| derives to the AccountRoot's `RegularKey` | accept | **refuse** `regular_key_unverified` (503, retryable) |
| anything else | refuse `pubkey_account` | refuse `pubkey_account` |

The rule applies to every proof: `walletconnect` and `agent` sign-in, and
wallet linking (`link_proof`), since a RegularKey has the same on-ledger
authority as the master key.

Refusing a proof signed by a *disabled* master key is new. A leaked master key
that its owner disabled can no longer sign in as that account. The ledger
already refuses such a key for real transactions, so the proof now matches
what the ledger accepts.

### Revocation

A RegularKey only helps if revoking it ends the sessions it created. This
matters: a live web session can, with no further signature, equip, harvest,
post Closet asks and fill any standing Closet bid, and a fill can sell units
at a lowball price.

- **Token contents.** A token minted from a RegularKey proof carries
  `key: "regular"` and `signer: <derived address>`.
- **The check.** For such a token, `require_wallet` confirms that the
  AccountRoot's `RegularKey` still equals `signer`. It uses `account_info` on
  the validated ledger, cached for at most 60 s per wallet.
- **Definitively removed or changed:** 401 `key_revoked`, and the token is
  added to the `revoked_sessions` denylist.
- **On a lookup error,** the last cached answer stands. There is always one,
  because sign-in itself verified the key.
- **Master-key tokens** are unchanged.

## 3. `GET /api/rarity/supply`

Public, unauthenticated, and cached 60 s per network like the leaderboard.

```json
{
  "network": "mainnet",
  "as_of": 1790000000,
  "n_live": 5242,
  "counts": { "Head": { "Pirate Hat": 41, "None": 612 }, "Eyes": { "...": 1 } }
}
```

**Shared helper.** `leaderboard.live_trait_table(oconn)` returns
`(table, freq)`:
- `table`: `dict[nft_id, (nft_number, pairs)]`;
- `freq`: `Counter[(trait_type, value)]`.

It comes from **one** `SELECT`, the same rows and parsing `_nft_rarity` uses
today. `_nft_rarity` scores from `table` and `freq`, so the board and its
counts come from one read even while the listener writes. The handler reports
`n_live = len(table)` (tokens with zero parsed pairs included, as the board
counts them) and `counts` from `freq`.

Values are the board's parsing, with no further normalization, including
quirks such as the live characters that store an empty `Accessory` as `''`.
A player optimizing against this endpoint sees exactly what the board scores.
Network: `config.XRPL_NETWORK`.

## Testing

- **Agent sign-in:**
  - With the flag off: 503 `agent_disabled`.
  - A good proof yields a `provider:"agent"` session.
  - An agent row refuses a `webapp`-memo proof, and a walletconnect row
    refuses an `agent`-memo proof.
  - A pending pre-deploy row with a NULL provider redeems as walletconnect.
  - Replay returns 409, and rate limits are shared.
- **Dispatch:**
  - An agent session's payload becomes a sign request, while a cross-wallet
    `Account` downgrades as today.
  - The discovery-contract test per flow.
  - The Closet Market bid and ask-buy refuse agent sessions.
  - Sign-request TTL expiry.
- **Memos:** each of these carries `platform=agent`:
  - an agent session's user-signed payload;
  - an agent equip's character modify **and** its Closet modify;
  - an agent harvest;
  - an agent Closet accept payload;
  - an agent claim payout;
  - a resumed session object.

  A human web equip and a human claim still carry `backend`, and the sponsored
  burn memo is byte-identical to before. The closed enum rejects unknown
  values.
- **Proofs**, with `account_info` stubbed for each table cell:
  - master accepted, and refused when disabled;
  - RegularKey accepted, and a foreign key refused;
  - on lookup error, master accepted and RegularKey refused (503);
  - a foreign-key link proof writes **no** `wallet_proof_links` row.
- **Revocation:**
  1. Set a RegularKey, sign in, then remove the key on-ledger.
  2. The next `/api/equip`, `/api/closet/ask` or `/api/closet/bid/{id}/fill`
     returns 401 `key_revoked` within 60 s.

  Master tokens are unaffected, and a lookup error keeps the cached answer.
- **Supply:** shape and 60 s cache. **Parity:** scores recomputed from the
  endpoint's table equal the board's output on the same fixture.
- No new stores, so `conftest.py` needs no new pins. The new `provider`
  column self-migrates.

## Docs (same PR)

- **CLAUDE.md:**
  - Add `agent` to the memo platform enum description.
  - Replace "RegularKey is NOT accepted … only the master key can prove
    ownership" with the §2 table and the revocation rule.
  - Fix the stale sweep sentence ("now started when `ECONOMY_ENABLED or
    config.wc_enabled()`") to "always started".
  - Add `AGENT_SIGNIN_ENABLED=0` to the env block and to
    `docs/ops/env.staging.example`.
  - Mention `GET /api/rarity/supply`.
- **The #447 WalletConnect design spec:** add a note that its RegularKey
  limitation is superseded.

## Rollout

1. **Wait for the fly's Phase 0 verdict** (operator decision). If the fly is
   shelved, the operator decides separately whether this PR is worth shipping
   on its own merits (RegularKey sign-in and revocation help any user).
2. **Three PRs** to `main` (operator decision, 2026-09-23), each reviewed by
   both bots; staging deploys through the deployer:
   - **A** `GET /api/rarity/supply` (§3);
   - **B** RegularKey-aware proofs and revocation (§2);
   - **C** the `agent` provider, client-signed dispatch and `platform=agent`
     memos (§1).

   The fly needs all three before its testnet rehearsal.
3. Staging: set `AGENT_SIGNIN_ENABLED=1` in `~/LFG-staging/.env`, then
   `pm2 restart stg-activity --update-env`. An `.env`-only change moves no
   branch, so the deployer never restarts anything, and the flag is read at
   import. The fly's testnet rehearsal exercises the whole surface.
4. Promote. When the fly goes live on mainnet, set the flag in the prod `.env`
   and run `pm2 restart lfg-activity --update-env`.

## Out of scope

- Opening Closet Market bids and ask-buys to client-signed providers.
- A pre-existing bug: the owner's bid-accept payload
  (`create_accept_buy_offer_payload`) sets no `Account`, so client-signed
  sessions fall back to Xaman. It hits Joey users today too; track it
  separately.
- "Log out everywhere" for master-key sessions (a possible follow-up).
- Any agent-specific endpoint, privilege, allowlist or rate tier: agents are
  users.
- Swap-chooser previews (separate task) and Builder pricing (operator's v2).

## Amendment 1 (2026-09-23): verified against `main` at `0415a63a`

Three code sweeps checked every reference above against current `main`. Facts
the spec had wrong are corrected here; the **decisions** are marked, and the
plans in `docs/superpowers/plans/2026-09-23-agent-users-{a,b,c}-*.md` follow
this section wherever it and the text above disagree.

### A (`/api/rarity/supply`)
- `_nft_rarity` has no cache of its own; the 60 s caches live in
  `lfg_service/app.py`. The endpoint gets its own per-network cache.
- `json.loads` of an `attributes_json` of `null` raises `TypeError` in the
  board today (only `ValueError` is caught). `live_trait_table` skips any
  value that doesn't parse to a list, for the board and the endpoint alike.
- CLAUDE.md has no endpoint list; the endpoint is documented next to the
  `/api/leaderboard` API bullet.

### B (RegularKey proofs and revocation)
- `verify_proof` gains `authority: KeyAuthority | None = None`. `None` keeps
  today's master-key-only rule, so every existing caller and test is
  unchanged. No `KeyAuthority`, `RegularKey` or `lsfDisableMaster` handling
  exists yet; `lsfDisableMaster` is read from `account_flags.disableMasterKey`
  first, then the `Flags` bit `0x00100000`, as `disallows_incoming_nft_offers`
  does for its flag.
- `account_info` "not found" (`actNotFound`) counts as a successful lookup of
  an account with no RegularKey and master enabled.
- Today every `ProofError` is a 400 `bad_proof`. `regular_key_unverified`
  becomes a **503** `{"code": "regular_key_unverified"}` (retryable); every
  other refusal, `master_disabled` included, stays a 400 `bad_proof` with
  the reason logged.
- `_redeem_proof` is shared by sign-in and wallet linking, so the key rule
  covers both (as §2 intends). The link flow's `on_verified` still runs only
  after the key check.
- `_finish_web_signin(wallet, provider, key_info=None)`: the new parameter is
  optional (tests call it with two positional arguments).
- **Decision: the revocation check lives in `require_auth`, not
  `require_wallet`.** A check in `require_wallet` would miss every
  `require_auth`-only endpoint (`/api/me`, status, cancel, regenerate, bulk
  unit accept). `require_auth` wraps all of them. Dev mode keeps bypassing it.
- **Decision: the revocation cache is seeded at sign-in, and a lookup error
  with no cached answer is a 503 `key_unverified`.** "There is always a cached
  answer" holds only within one process; after a restart the cache is empty.
  Failing closed here matches sign-in's own `regular_key_unverified` rule.
- Revoking uses the existing `revoke_session_token(token)`, which needs the
  raw token; `require_auth` has it from the `Authorization` header.

### C (`agent` provider, dispatch, memos)
- The start response already carries `expires_at` and `provider`; the agent
  arm returns the walletconnect arm's shape with `provider: "agent"`.
- `_redeem_proof` returns only the wallet today, and `handle_web_signin_proof`
  hard-codes `"walletconnect"`. `_redeem_proof` returns the row's provider (and
  B's signer) so the handler labels the token from the row. Wallet-link rows
  have no provider and keep verifying against `webapp` memos.
- `sign_requests.create` gains `provider=None`; the column migrates with this
  table's existing try/except "duplicate column name" pattern (`store.py`).
- **Decision: wallet linking gets no agent arm** (out of scope). An agent
  proves one wallet; its link start stays gated on `wc_enabled()`.
- An unknown `provider` still falls through to the Xaman arm, as today.
- Agent rows reuse `WalletConnectProvider` for dispatch; `get_provider` has no
  `"agent"` entry and needs none. Only two `current_provider() == "walletconnect"`
  checks exist (the two Closet Market refusals); every other `"walletconnect"`
  literal (the sign-in and link arms, `/api/config`, the registry name,
  `sign_mode`, the client's Joey restore) stays as it is. The refusal copy
  "need Xaman for now" becomes provider-neutral.
- **Provider source.** No session or job object has a `provider`. **Decision:
  every memo site reads it from `signing_context.current_provider()`**, which
  `require_auth` sets for the request and every task it spawns, through a new
  `memos.platform_for(surface_platform)` helper (`PLATFORM_AGENT` when the
  current provider is `agent`, otherwise today's value). No signatures change.
- The 30 call sites: counts are right, but none pre-builds `memos_json`, and
  **10 are backend-signed** (`mint_nft`, `create_nft_offer`/`ensure_offer`,
  `modify_nft`, `burn_nft`, `prepare_sponsored_mint`, the shop's `mint_fn`
  and `offer_fn`). They already stamp the surface platform (e.g. `webapp`),
  so they switch the same way. The sponsored `NFTokenMint` gets relabelled
  too; its recovery is by hash, not by memo.
- Backend-signed economy ops: `modify_nft`, `mint_nft`, `burn_nft`,
  `create_nft_offer` and `create_accept_offer_payload` already accept
  `platform=`; only `build_economy_deps` never passes it. It gains
  `platform=None` (None = `memos.backend_platform()`, read from the signing
  context when the deps are built, i.e. inside the request that starts the
  session), passed to **every** op it builds (character and Closet modifies,
  Closet/character/trait mints and burns, the offer and accept functions).
  So `_schedule`, `start_closet` (which bypasses `_schedule`) and batch harvest
  need no change. `_accept_or_skip` builds *user-signed* accept payloads
  that default to `backend`; an agent session labels them `agent`, humans
  keep `backend`.
- **Settlement** (`build_settlement_deps`) isn't session-initiated and keeps
  `backend`, pinned explicitly: it also runs inside a buyer's buy-status
  request, where the context would otherwise say `agent`.
- BRIX claims: `send_brix_claim` gains `platform=memos.PLATFORM_BACKEND`, and
  `_claim_one_wallet` passes `memos.backend_platform()`. It runs inside the
  request (`handle_brix_claim`) or inside the claim-all task, which inherited
  the request's context, so both paths get the session's value. Test stubs
  with the fixed signature are updated.
- **Persistence (corrected).** Only bulk-mint jobs and burn2mint sessions
  persist and resume; mint, swap, market and economy sessions are in memory.
  **Decision:** those two persist the session's `provider` and wallet, and
  their resume path restores `signing_context` from them. Today a resumed job
  runs with the default context (Xaman), so a Joey user's resumed bulk mint
  already dispatches to Xaman; this fixes that too.
- Discovery contract, per flow (field that carries `lfg-wc://<id>`):
  - mint payment `payment_link`, mint delivery accept `accept_deeplink`;
  - bulk-mint payment `payment_link`, bulk unit accept `link`;
  - pending-offer accept `link`;
  - `POST /api/closet` accept `accept` (only while an offer is pending);
  - harvest delivery accept `accept` on `GET /api/harvest/{id}` (legacy,
    non-mutable characters only; a mutable harvest has no accept);
  - BRIX trustline `uuid` and `xumm_url`.

  Every id is `wc-`-prefixed, so the contract is `lfg-wc://wc-…`.
