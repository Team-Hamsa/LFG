# BRIX trustline flow — implementation plan (#441)

Spec: `docs/superpowers/specs/2026-08-24-brix-trustline-flow-design.md`.
Status: implemented in PR #442 (with the tri-state lookup fix from #440).

## Steps

1. **Config** — `BRIX_TRUSTLINE_LIMIT` (`lfg_core/config.py`, default 1e9),
   documented in `CLAUDE.md`.
2. **Lookup** — `xrpl_ops.get_trustline_state` returns
   `PRESENT | ABSENT | UNKNOWN`; an `account_lines` *error response* (xrpl-py
   does not raise) is UNKNOWN, never ABSENT. `handle_brix_claim` maps
   ABSENT → 409 `trustline_required`, UNKNOWN → 503 `claim_unavailable`.
3. **Payload** — `xumm_ops.create_trustset_payload` via `_create_xumm_payload`
   (SourceTag, `action=trustset` memo, 15-min expiry, push, `Account` pinned).
4. **Endpoints** — `POST /api/brix/trustline` (PRESENT → `already_set`),
   `GET /api/brix/trustline/{uuid}` (owner-scoped; `signed` only on validated
   `tesSUCCESS`, `validating` until then, `rejected` with `signer_mismatch` /
   `tx_failed`); in-memory records pruned by `BRIX_TRUSTLINE_TTL`.
5. **Client** — `#trustline-panel` (registered in `ALL_PANELS`), lock label
   "Set BRIX trustline" stays clickable, `trustlineView()` in `brix_pure.js`;
   trait Buy's 409 opens the same panel and re-issues the buy on success.
6. **Tests** — payload shape, endpoint state machine, pure-JS views, DOM
   wiring; cache-buster ratchets bumped.
7. **Ops** — staging smoke with a testnet wallet lacking a BRIX line, then
   `scripts/promote.sh`.
