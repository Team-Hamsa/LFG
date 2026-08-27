# Wallet↔User-Token Correlation (#445) — Design

Part of the User Profiles epic (#444), step 1. Turns the XUMM
`application.issued_user_token` we already capture into an automatic
cross-wallet linking key, so wallets signed from the same Xaman install join
one identity bucket — including web-surface wallets, which today have no
unifying key at all (`platform_user_id` IS the wallet, #240).

## Why the user token

- Scope: per XUMM app + Xaman **user** — the same token is issued no matter
  which r-address in that Xaman install signs. Zero extra user friction.
- It rotates (expires 30 days after the user's last signed payload), so
  correlation is by **recorded co-observation**, never live-token equality:
  wallets that ever shared a token stay linked; a new token bridged by a
  common wallet extends the same bucket.
- It is a push credential, so the correlation table stores only a
  **sha256 hash** of it. `identities.user_token` keeps the raw value for push
  delivery, unchanged.

## Schema

New table in the app DB (`lfg_nfts.db`), created in
`identity.ensure_identities_table()` next to `identities`/`wallet_links`:

```sql
CREATE TABLE IF NOT EXISTS wallet_token_links (
    token_hash TEXT NOT NULL,   -- sha256 hex of the raw issued_user_token
    wallet     TEXT NOT NULL,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen  TIMESTAMP,
    PRIMARY KEY (token_hash, wallet)
)
-- + index on wallet
```

Append-only: rows are never updated except to bump `last_seen`, never
deleted. Mirrors the `wallet_links` posture (#206).

## Recording

One new function, `identity.observe_token(wallet, raw_token)` — hashes and
upserts. Called only from the existing sites that persist a captured token
from a **signed** payload where signer == session wallet is already verified
(the link evidence is a signature by that wallet, never client-asserted):

- `handle_signin_status` (sign-in capture — covers web signin, the key case)
- `_persist_issued_user_token` (the #212 every-payload recapture used by the
  mint/market status handlers)

No new capture logic; no new XUMM fields read.

## No seeding

`identities.user_token` is deliberately NOT seeded into the correlation
table: `link()` preserves the stored token across a wallet relink, so a
boot-time seed would pair a token with a wallet it was never observed
signing as — a bucket merge without signed evidence. Observations come only
from `observe_token()` at verified-signer capture sites; existing users
bucket organically as they sign (#212 recaptures the token off every signed
payload).

## Bucket walk

`identity._bucket_on_conn`'s frontier loop gains a second edge type:
wallet → `wallet_token_links.token_hash` → wallets, unioned with the
existing identity→`wallet_links` edges, iterated to fixpoint. New wallet-keyed
entry point `bucket_for_wallet(wallet)` (web callers only have a wallet).
Bucket id stays deterministic: smallest `[platform, platform_user_id]` member
key; a wallets-only bucket (no platform identities) uses `["wallet",
<smallest wallet>]`.

## Out of scope

No API/UI changes, no claim-all (#446), no merge/unlink UX, no non-Xaman
providers (#447). After this change `bucket_for` is correct and testable but
still uncalled by the service.

## Security

- Links derive only from signed-payload evidence.
- Raw tokens never land in `wallet_token_links`.
- A shared physical device could bucket two humans; acceptable at this layer
  because nothing consumes buckets yet — #446/#447 add confirmation UX where
  buckets become user-visible or actionable.

## Testing

Unit tests (sqlite tmp DB, like the existing identity tests): observe/hash/
append-only semantics; seed idempotency; transitive bucketing (A+B share a
token → one bucket; rotation — two hashes bridged by a common wallet — does
not split; token edges union with identity edges); raw token absent from the
table; capture-site wiring (signin + payload-status persist paths call
observe_token).
