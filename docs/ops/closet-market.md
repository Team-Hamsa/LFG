# Closet Market (#443) — ops runbook

Off-ledger trait asks + TokenEscrow-backed bids. Ships dark (`CLOSET_MARKET_ENABLED=0`).

## 1. Ledger prerequisites (once per network)

Check first (read-only):

```bash
.venv/bin/python scripts/closet_market_setup.py --network mainnet
```

| fact (verified 2026-09-14) | value |
|---|---|
| BRIX issuer | `rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px` — master disabled, RegularKey `rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ` (= distributor) |
| `lsfAllowTrustLineLocking` | NOT set |
| app wallet (`SIGNING_ACCOUNT`) | `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ` — BRIX line limit 258,055, balance 45,509 |

1. **Issuer flag** (signed by `BRIX_DISTRIBUTOR_SEED`, `Account` = issuer). One-way in practice: it cannot be cleared while any BRIX sits in an escrow.
   ```bash
   .venv/bin/python scripts/closet_market_setup.py --network mainnet --apply-issuer-flag
   ```
2. **App wallet limit** → `BRIX_TRUSTLINE_LIMIT` (EscrowFinish into the app wallet fails once balance + bid > limit):
   ```bash
   .venv/bin/python scripts/closet_market_setup.py --network mainnet --apply-app-limit
   ```
3. Testnet: `--network testnet --apply-issuer-flag` (the SEED account is issuer + app wallet; no limit step).

## 2. Configuration

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```
CLOSET_MARKET_ENC_KEY=<fernet key>     # REQUIRED; losing it strands every open bid until CancelAfter
CLOSET_MARKET_FEE_BPS=700              # 0 <= bps < 10000
CLOSET_BID_TTL_SECONDS=604800
CLOSET_MARKET_LEDGER_MARGIN=40
CLOSET_USER_TX_LEDGER_WINDOW=300       # LastLedgerSequence window pinned on user buy/bid txs (~20 min)
CLOSET_MARKET_ENABLED=1                # last — new orders only
```

Back up `CLOSET_MARKET_ENC_KEY` with the other secrets. Without it the backend can
neither fill nor refund an open bid early, and the settlement sweep stops
entirely — including the EscrowCancel of expired bids. Bidders must then cancel
their own escrows after `CancelAfter` (EscrowCancel; anyone may submit it).

## 3. Audit cron

```bash
pm2 start ecosystem.prod.config.js --only lfg-closet-market-audit && pm2 save
.venv/bin/python scripts/audit_closet_market.py --network mainnet --onchain   # manual; exit 1 = drift
```

## 4. Kill switch & rollback

- `CLOSET_MARKET_ENABLED=0` + restart: no new asks/bids/buys/fills. Cancels, status
  polls and the 2-minute settlement sweep keep running (they need only
  `ECONOMY_ENABLED=1` and the enc key), so nothing escrowed is stranded.
- Never delete `closet_orders` / `closet_fills` rows: a code rollback must keep
  both tables (open escrows are only findable through them).

## 5. Reading a stuck fill

Journals: `$ECONOMY_RECORDS_DIR/closet-fill-<id>.json` and `closet-order-<id>.json`.

| state | meaning | action |
|---|---|---|
| `indeterminate` | a backend tx outcome is unknown (or a crash left a write-ahead intent: `pending_phase` set) | if `CLOSET_MARKET_ENC_KEY` is missing, restore it and let the settlement sweep run first; once the sweep runs, none — never edit the row by hand (it resolves when the validated ledger passes `pending_lls`: memo `lfg:closet_<phase>:<id>` found = landed, absent = retried) |
| `asset_moved` + attempts > 0 | forward payment failing | check app wallet BRIX balance (audit `owes` line); or the counterparty removed their BRIX trustline (audit `payout failing` line) — the forward retries until they re-add it |
| `paid` + attempts > 0 | Closet mirror failing | check CDN / NFTokenModify; the listener will not rebuild this owner until mirrored (and for 5 minutes after) |
| `paid` + error `Closet update is waiting for a Closet to catch up with the ledger…` | that side's Closet mirror may lack a version already on-chain, or the history archive can't be read to check, so its overwrite is refused (no attempt counted) | see §6 |
| `refund_pending` + attempts > 0 | refund failing | app wallet balance; or the counterparty removed their BRIX trustline (audit `payout failing` line) — the refund retries until they re-add it |

Any fill or order with `pending_phase` set resolves itself: memo `lfg:closet_<phase>:<id>` found = landed, validated ledger past `pending_lls` with nothing found = retried; never edit these columns by hand.

## 6. A fill waiting on a stale Closet mirror (#530)

The mirror step rewrites each side's whole Closet token from `closet_assets`, so a
mirror that lacks a version already on-chain would erase that version (#522). Like
the economy flows, the step refuses first, with nothing uploaded or submitted, when
the owner is `mirror_pending` (a committed Closet modify whose mirror write failed)
or when the history archive holds a newer version of the Closet than the mirror.

It also refuses when the history archive (`history_<net>.db`) can't be read at all:
the file is missing, corrupt, or the read fails, for example on a lock that outlasts
the 5-second timeout. The user-facing flows skip the check in that case (#529).
The settlement doesn't: it retries anyway, so waiting only delays the mirror, while
an unchecked overwrite could erase a newer version. The archive is shared, so this
pauses the mirror step of every fill, for both sides, until it can be read again.
The next sweep pass after that goes ahead. The residual stall below does not apply
to this case.

What it looks like:

- `lfg-activity` log, a WARNING on every sweep pass:
  `closet fill <id>: Closet update for <owner> (<side>) is waiting (closet_mirror_behind|closet_mirror_pending|closet_mirror_unverified): …`.
  A `closet_mirror_behind` refusal comes after
  `Closet mirror for <owner> (<nft_id>) holds the ledger-<N> version but the archive has ledger <M>; refusing a full overwrite`,
  and a `closet_mirror_unverified` one after
  `Closet stale-mirror check for <owner> cannot run: history archive <path> unavailable (<error>); refusing a full overwrite`.
- The fill row: `state = paid`, `<side>_mirrored = 0`, `attempts` unchanged, and
  `error = Closet update is waiting for a Closet to catch up with the ledger; will retry automatically`.
  The error is written once, so `updated_ts` stops moving. After an hour the nightly
  audit reports `stuck in paid for <N>s`. It never reports `payout failing` for this.
- Users: the trade is complete (the buyer's Closet in the DB holds the unit, and the
  seller has been paid). The fill status stays `done` with `fill_state: paid` and the
  error above; Mine shows "Updating Closets".

The fill finishes by itself once the owner's mirror is current and not
`mirror_pending`: the sweep retries every 2 minutes, and the next pass mirrors the
side and clears the error. Nothing needs doing on the fill itself.

**Residual stall.** While the fill is unmirrored, the owner's mirror does not catch
up on its own. The listener and `backfill_economy.py` skip rebuilding
`closet_assets` for an owner with an unmirrored fill (#443), and the same two checks
refuse that owner's economy flows, so nothing brings the contents current. The fill
stays `paid` but unmirrored until an operator reconciles that owner. No assets are
lost while it waits: the token keeps its newer version, and the fill row records the
move. It is safe to leave it waiting.

To reconcile one owner:

1. Find the newest version of their Closet: `history_store.latest_uri_version` on
   `history_<net>.db`. Its URI hex-decodes to the metadata URL.
2. Work out what the owner should hold: that version's contents, plus the move of
   each of the owner's unmirrored fills (seller −1 / buyer +1 of its slot and value)
   that the version does not already reflect. To tell, match the version's tx hash
   (its `nft_events` row) against `sync_tx_hash` / `pending_tx_hash` in the owner's
   op journals in `$ECONOMY_RECORDS_DIR`: an op that ran after the fill's asset move
   composed the version from contents that already include the move.
3. Write the contents with `economy_store.set_closet_contents`, which also clears
   `mirror_pending`. **Then** point the mirror at that version with
   `economy_store.set_closet_token(..., applied_ledger_index=<its ledger>)`. Keep this
   order: if the URI and stamp go first, a sweep pass in between overwrites the token
   from the old contents.

**Do not run `scripts/backfill_economy.py` to clear this.** For an owner with an
unmirrored fill it keeps the stale contents but records clio's current URI. That makes
the stale mirror look current, so the next sweep pass overwrites the token and erases
the newer version on-chain.
