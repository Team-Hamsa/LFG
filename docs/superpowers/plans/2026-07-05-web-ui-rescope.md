# Web UI (#42) Re-scope — Implementation Plan

Spec: `docs/superpowers/specs/2026-07-05-web-ui-rescope-design.md`.
Scope: ONLY the two genuine gaps — (A) standalone-browser wallet session,
(B) per-wallet profile page. Admin dashboard deferred; collection browser
dropped; HTTPS exposure is #89 Part B ops.

## House rules (apply to every task)

- **TDD**: write the failing test first, watch it fail, then implement.
- **Draft PR + CodeRabbit**: `gh pr create --draft`; flip ready only when
  settled; do not merge until CodeRabbit findings are handled.
- **Env-guard preamble**: every new test file that imports `lfg_core` or
  `lfg_service` at module top MUST start with this block, copied **verbatim**
  from `tests/test_seasons.py:1-18`:

  ```python
  # Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
  # IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
  # test_smoke.py uses so collection order can't strand them. (Copy the block
  # verbatim from tests/test_server_identity_wiring.py — same keys/values.)
  import os

  os.environ.setdefault("XUMM_API_KEY", "test")
  os.environ.setdefault("XUMM_API_SECRET", "test")
  os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
  os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
  os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
  os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
  os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
  os.environ.setdefault("LAYER_SOURCE", "local")
  os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
  ```

- **Fail-closed auth**: an unverified/ambiguous Xaman result NEVER yields a
  session token; unknown payload uuid → 404; over-limit → 429. Test the
  denial paths first, before the happy path.
- **Honesty**: mark each verification as VERIFIED (ran it, saw output) vs
  ASSUMED (needs the real Xaman/browser). Two known ASSUMPTIONS to convert
  early: `create_signin_payload(return_url=None)` works; `identity_store.link`
  accepts a wallet address as `platform_user_id`.

## Task 1 — spike: verify the two assumptions (no PR)

Read `lfg_service/xumm_ops.py::create_signin_payload` and
`lfg_service/identity_store.py::link`; if `return_url=None` or wallet-as-id
is unsupported, adjust the design (e.g. omit return_url key / widen link) and
note it in the PR description. Output: a VERIFIED/adjusted note in Task 2's
PR body.

## Task 2 — gap A backend: `platform="web"` session bootstrap (draft PR 1)

Branch `feat/web-signin`.

1. **Tests first** — `tests/test_web_signin.py` (env-guard preamble; use
   `aiohttp.test_utils` app client with `xumm_ops` monkeypatched, same style
   as existing service tests):
   - POST `/api/web/signin` with XUMM mocked → 200 `{uuid, signin_link}`;
     no Authorization header required; assert the mocked payload-create was
     called with XUMM `options.expire = 10` (payload expiry).
   - GET `/api/web/signin/{unknown}` → 404.
   - Status while pending/opened → `{state}` and **no** `session_token` key
     (unsigned → no session).
   - Signed with invalid/empty account → no token (fail-closed).
   - Expired payload → `{state:"expired"}`, no token; once pruned the uuid
     → 404 (expired → no session).
   - **Throttle → 429** (injectable clock / frozen time): 6th pending
     payload from one IP → 429 with `Retry-After`; 11th creation in the
     10-min sliding window from one IP → 429; a different IP is unaffected;
     201st globally-pending → 429. Client IP from `X-Forwarded-For`
     (left-most) only when `WEBAPP_TRUST_PROXY` is set, else socket peer —
     test both branches (spoofed XFF without the flag must NOT reset the
     throttle key).
   - Signed with valid account → `{state:"signed", session_token, wallet}`;
     token round-trips through `verify_session_token` with
     `platform == "web"`, `id == wallet`.
   - **Signed-account binding**: token wallet equals the account the mocked
     `get_payload_status` returns — and a request body containing a
     `wallet`/`account` field is ignored (send one; assert the token still
     binds to the XUMM-reported account, never client-asserted).
   - **One-time exchange → 410**: second GET for the same uuid after a
     successful token issue → 410 Gone with no token (tombstoned; distinct
     from never-existed 404).
   - No legacy Users write: assert `register_user` NOT called (monkeypatch
     sentinel) — web platform must not touch the discord-keyed table
     (guard documented at lfg_service/app.py:669-674).
   - `_resolve_wallet("web", "rVALID...")` returns the id;
     `_resolve_wallet("web", "junk")` returns None → `require_wallet` 400.
2. **Implement** in `lfg_service/app.py`: `web_signin_payloads` dict
   (`created_at`, `client_ip`) + 10-min prune + tombstone set for issued
   uuids (mirror `_prune_signin_payloads` app.py:933); per-IP sliding-window
   throttle + global pending cap; the two handlers — create passes XUMM
   `options.expire=10`, exchange deletes the record **before** returning the
   token (issued exactly once) and tombstones the uuid for 410-on-replay;
   the `platform == "web"` branch in `_resolve_wallet` (app.py:283); and
   `identity_store.link("web", wallet, "", wallet)` on success. Register
   routes in `create_app()` (app.py:1253+).
3. Run `pytest tests/test_web_signin.py` then the **full suite** (collection-
   order guard is exactly why); `ruff format` (pre-push runs it anyway).
4. Draft PR; after CodeRabbit review + fixes, `gh pr ready` (respect the
   4/hour ready budget).

## Task 3 — gap A frontend: Connect-wallet boot panel (draft PR 2, stacked)

Branch `feat/web-signin-ui` on top of Task 2.

1. **Tests first**: the client is no-build vanilla JS with no JS test rig —
   test the seam that IS testable: extend `/api/config` test coverage if the
   boot decision needs a new flag; otherwise document manual verification
   steps in the PR (WEBAPP_DEV_MODE=0, plain browser on :8176 → connect
   panel appears; scan → mint home). Mark these MANUAL-VERIFIED with a
   pasted transcript/screenshot — do not claim done without doing it.
2. **Implement** in `webapp/client/` (+ `index.html`): detect "no host
   platform" at boot (not Discord iframe, no Telegram initData, `dev_mode`
   false from `/api/config` app.py:1016) → show `connect-panel` (add to
   `ALL_PANELS`, app.js:155); drive POST `/api/web/signin`, render QR via
   existing `/api/qr.png`, poll status, store `session_token` in
   `sessionStorage`, then fall into the normal `/api/me` boot path
   (app.js:1424). Expired → "QR expired, retry" state.
3. Real-flow verification on testnet with an actual Xaman scan (fail-closed
   claim requires seeing the pending→signed transition live). VERIFIED note
   in PR body.

## Task 4 — gap B backend: `GET /api/profile/{wallet}` (draft PR 3)

Branch `feat/profile-api` (independent of Task 2/3 — can parallelize).

1. **Tests first** — `tests/test_profile_api.py` (env-guard preamble; seed
   temp `onchain_*.db` / `history_*.db` fixtures the way existing
   leaderboard tests do):
   - Invalid address → 400; valid-but-unknown wallet → 200 with empty
     nfts/zero stats (public endpoint, no existence oracle beyond the chain).
   - Known wallet → owned live editions (is_burned=0 only), correct
     mint/swap/build counts from `nft_events`, recent events ordered desc.
   - Response NEVER contains platform identities (privacy invariant of
     `/api/account`, app.py:453-462) — assert absence of `identities` key
     and of any discord/telegram id, even when identity rows exist.
   - Cache: second call within TTL doesn't re-query (monkeypatch counter);
     keyed per wallet+network.
   - No auth required (no Authorization header in any test call).
2. **Implement**: handler in `lfg_service/app.py` reusing `_lb_display_name`
   (app.py:497) and the `_LB_CACHE` put/evict pattern (app.py:465-484);
   queries via `lfg_core/history_store.py` + on-chain index. Route in
   `create_app()`.
3. Full suite + draft PR + CodeRabbit as above.

## Task 5 — gap B frontend: profile panel + deep links (draft PR 4, stacked on 4)

1. Add `profile-panel` to `ALL_PANELS`; render wallet header (display
   handle), NFT grid (reuse the swap-grid card rendering, app.js:629-660),
   stats row, recent-events list.
2. Entry points: leaderboard row click → profile; "My profile" when
   `me.wallet` set; `#/profile/rXXX` hash routing handled at boot and on
   `hashchange`. Any *absolute* share/copy-link URL builds from
   `PUBLIC_SHARE_BASE_URL` (per `2026-07-05-x-integration-design.md`
   :229-256 — never the request Host header); unset ⇒ relative hash links
   only, no absolute self-URL emitted.
3. Manual verification (dev-mode harness + testnet data) documented in PR
   body with the same VERIFIED/ASSUMED honesty.

## Task 6 — close-out

1. Edit issue #42 body to the re-scoped text in the spec §6
   (`gh issue edit 42 --repo Team-Hamsa/LFG --body-file …`). The body must
   name the sibling 2026-07-05 specs that absorbed the collection-viewer
   scope (marketplace, x-integration §6.2 OG page, amm-backend §4).
2. Commit spec+plan (this file + the design doc) and comment permalinks on
   #42 per the CLAUDE.md brainstorming→issue-link rule (blob URLs at the
   commit SHA).
3. File the deferred-admin follow-up issue referencing spec §5 rationale.
4. Note in #89 that gap-A public exposure rides its Part B ops.

## Ready-for-review sequencing

Four draft PRs; flip ready in ≤4/hour batches: PR1 → PR3 first (auth spine),
then PR2 → PR4 (UI). Each waits for CodeRabbit before merge.
