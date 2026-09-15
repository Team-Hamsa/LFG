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
| `indeterminate` | a backend tx outcome is unknown (or a crash left a write-ahead intent: `pending_phase` set) | none — resolves when the validated ledger passes `pending_lls` (memo `lfg:closet_<phase>:<id>` found = landed, absent = retried) |
| `asset_moved` + attempts > 0 | forward payment failing | check app wallet BRIX balance (audit `owes` line); or the counterparty removed their BRIX trustline (audit `payout failing` line) — the forward retries until they re-add it |
| `paid` + attempts > 0 | Closet mirror failing | check CDN / NFTokenModify; the listener will not rebuild this owner until mirrored (and for 5 minutes after) |
| `refund_pending` + attempts > 0 | refund failing | app wallet balance; or the counterparty removed their BRIX trustline (audit `payout failing` line) — the refund retries until they re-add it |

Any fill or order with `pending_phase` set resolves itself: memo `lfg:closet_<phase>:<id>` found = landed, validated ledger past `pending_lls` with nothing found = retried; never edit these columns by hand.
