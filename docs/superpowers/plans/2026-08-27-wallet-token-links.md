# Plan: Wallet↔User-Token Correlation (#445)

Spec: `docs/superpowers/specs/2026-08-27-wallet-token-links-design.md`.
TDD throughout — each task writes failing tests first.

## Task 1 — schema + observe_token
- `tests/test_identity_token_links.py`: table exists after
  `ensure_identities_table()`; `observe_token(wallet, token)` inserts
  (sha256 hex, wallet); re-observe bumps `last_seen` only; raw token never
  appears in any column; None/empty token is a no-op.
- Implement in `lfg_service/identity.py`: CREATE TABLE + wallet index in
  `ensure_identities_table`; `_token_hash()`; `observe_token()`.

## Task 2 — seed from identities.user_token
- Tests: identities rows with user_token get seeded observations on
  `ensure_identities_table()`; re-run adds nothing; NULL-token rows skipped.
- Implement: `INSERT OR IGNORE ... SELECT wallet, user_token FROM identities
  WHERE user_token IS NOT NULL` (hashing in Python — iterate rows).

## Task 3 — bucket walk over token edges + bucket_for_wallet
- Tests: two wallets sharing a token bucket together via
  `bucket_for_wallet`; rotation case (hash1: A+B, hash2: B+C → one bucket
  A,B,C); union with identity/wallet_links edges (Discord identity linked to
  wallet A, token links A↔B → bucket carries the identity and both wallets);
  deterministic bucket id incl. wallets-only form `["wallet", smallest]`;
  unknown wallet → None.
- Implement: extend `_bucket_on_conn` frontier with token edges;
  `bucket_for_wallet(wallet)`.

## Task 4 — wire capture sites
- Tests: `handle_signin_status` and the payload-status persist path
  (`_persist_issued_user_token`) call `observe_token` with the session
  wallet + captured token (monkeypatch-spy style, as existing capture tests
  do).
- Implement: add the calls (best-effort — an observe failure must never
  break signin/mint; log and continue).

## Task 5 — finish
- Full gate (ruff/mypy/pytest via pre-push), PR to Team-Hamsa/LFG referencing
  #445, babysit Greptile + CodeRabbit, reply on every finding thread.
