# Fee cover — testnet rehearsal (T22)

One real brokered fill of a Buy-now bid placed through LFG on staging, with its
fee-cover refund paid and audited, before a campaign is started on mainnet.
Testnet has no xrp.cafe, so `scripts/fee_cover_rehearsal.py` stands in for the
missing parties: a broker, a seller whose character is listed with that broker,
and an intermediate wallet that funds the seller. The bidder is a real testnet
wallet signed in to staging.

Spec: `docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md` · issue #515.

**Who does what.** Commands marked *ops* run on the box. Everything marked
*owner* is owner-only: the `.env` edit, the pm2 restart, campaign Start/Stop,
and signing the bid in a wallet.

## 0. Before you start

- Staging (`~/LFG-staging`, branch `main`) runs a build that includes #515.
  `stg-activity` and `stg-index-testnet` are online.
- Staging's `.env` is testnet: `XRPL_NETWORK=testnet`, and `SEED` is the
  testnet issuer (= `SIGNING_ACCOUNT`). `NFT_FLAGS` includes tfTransferable
  (8), and `NFT_TRANSFER_FEE` is above 0: with a TransferFee of 0 the refund is
  declined (§4).
- No fee-cover campaign is active on staging:
  `.venv/bin/python scripts/fee_cover_report.py --network testnet` prints
  `state: never_started` or `state: stopped`.
- **The bidder** is a testnet wallet in Xaman (or Joey, on the web surface),
  funded from the testnet faucet with a few XRP more than the bid. It must not
  be the issuer, the BRIX distributor, or in `SPONSORED_MINT_EXCLUDED_WALLETS`.
  It must share nothing with the seller (§4).
- **The state file** lives outside every checkout, for example
  `~/fee-cover-rehearsal/state.json`. It holds the seeds of three throwaway
  testnet wallets. The script writes it owner-only (0600) and refuses a path
  inside the repository.

Run every command from `~/LFG-staging`: the scripts read staging's `.env`, and
the app DB path is relative to the working directory.

The harness exits 2 on anything but testnet. Every transaction it submits
carries SourceTag `2606160021` and provenance memos with campaign
`fee-cover-rehearsal`. `setup` and `mint-to-seller` skip the steps they already
recorded, so one that failed part-way can simply be run again.

`XRPL_NETWORK` alone is not trusted, because an explicit `XRPL_JSON_RPC_URL`
wins over the network's defaults and an exported shell variable beats `.env`.
So before it touches the ledger, every subcommand asks each configured
JSON-RPC endpoint what it is: `server_info` must report `network_id` 1, and the
ledger-32570 anchor must match once the testnet archive has recorded one. Any
endpoint that is not testnet exits 2 with nothing signed; one that cannot
answer is excluded for that run.

## 1. Stage the listing (ops)

```bash
cd ~/LFG-staging
STATE=~/fee-cover-rehearsal/state.json
.venv/bin/python scripts/fee_cover_rehearsal.py setup --state $STATE
.venv/bin/python scripts/fee_cover_rehearsal.py mint-to-seller --state $STATE
.venv/bin/python scripts/fee_cover_rehearsal.py list --state $STATE --price-xrp 5
.venv/bin/python scripts/fee_cover_rehearsal.py print-allowlist --state $STATE \
  > ~/fee-cover-rehearsal/allowlist.json
```

- `setup` creates the broker, intermediate and seller wallets. The faucet funds
  the intermediate and the broker; the **intermediate** funds the seller
  (25 XRP). The seller's activation funder is therefore never the faucet that
  funded the bidder, so the two can't be linked by funder (§4).
- `mint-to-seller` mints one character the way the app's own mint does (issuer
  `SIGNING_ACCOUNT`, `NFT_TAXON`, `NFT_FLAGS`, TransferFee `NFT_TRANSFER_FEE`).
  The issuer then offers it to the seller for 0 drops (Destination = seller),
  and the seller accepts.
- `list` creates the seller's sell offer with Destination = broker, and prints
  the price a Buy-now bid clears at (5 XRP ask: 5.080733 XRP).
- `print-allowlist` prints the `BROKER_ALLOWLIST_PATH` overlay:
  `{"<broker>": {"name": "testbroker", "url_template": null, "broker_rate": 0.01589}}`.
  That rate is xrp.cafe's measured one.

Check that the staging listener indexed the character and the listing (allow a
minute or two):

```bash
NFT=<nft_id printed by mint-to-seller>
sqlite3 onchain_testnet.db "SELECT owner, is_burned FROM onchain_nfts WHERE nft_id = '$NFT'"
sqlite3 onchain_testnet.db "SELECT offer_index, seller, destination, amount_drops, is_live FROM market_listings WHERE nft_id = '$NFT'"
```

The owner must be the seller, and the listing must be live with the broker as
destination. If the character is missing, the staging listener's collection
(issuer `SWAP_ISSUER_ADDRESS`, taxon `ASSEMBLE_TAXON`) doesn't match the mint's
(`SIGNING_ACCOUNT`, `NFT_TAXON`), and staging's own mints aren't indexed either.
A listing only indexes once its character does; `scripts/backfill_market.py
--network testnet` re-sweeps listings after that.

## 2. Allowlist the broker and start a campaign (owner)

1. Add `BROKER_ALLOWLIST_PATH=/home/hamsa/fee-cover-rehearsal/allowlist.json`
   to `~/LFG-staging/.env`, then `pm2 restart stg-activity --update-env`.
2. Confirm browse shows the listing as an external, measured-rate row:
   ```bash
   curl -s 'localhost:8177/api/market/listings?kind=character&include_external=1' \
     | jq --arg nft "$NFT" '.rows[] | select(.nft_id == $nft) | {source, marketplace, broker_rate, clearing_xrp}'
   ```
3. Start a small campaign, in Discord `/admin` → 💸 Fee Cover on staging. If the
   staging bot isn't running, call the admin endpoint with staging's Discord
   service token instead:
   ```bash
   TOKEN=$(grep '^SERVICE_TOKEN_DISCORD=' ~/LFG-staging/.env | cut -d= -f2-)
   curl -s -X POST localhost:8177/api/admin/fee-cover/start \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"actor":"owner:t22-rehearsal","coverage_pct":100,"budget_xrp":"5","wallet_cap_xrp":"1","min_bid_xrp":"1"}'
   ```
   The response's `state` must be `active`.

## 3. Bid, fill, verify

1. **Owner.** Sign in to staging with the bidder wallet. Open the rehearsal
   character's external listing and press **Buy now**; the confirm dialog
   shows the fee-cover note. Sign the bid.
2. **Ops.** Wait for the bid's promise and note its offer index. Every bid that
   validates gets a promise row, whether it was covered or declined, so this
   query is the one that always answers:
   ```bash
   sqlite3 lfg_nfts_testnet.db "SELECT offer_index, bidder, state, reason, promised_drops FROM fee_cover_promises WHERE nft_id = '$NFT' ORDER BY created_at DESC LIMIT 3"
   ```
   The promise must be `open` to go on. A `declined` one carries its reason
   (§4) and there is nothing left to settle.

   A bid that was quoted as covered also has a `fee_cover_quotes` row, which is
   where its session id lives. A bid declined when it started
   (`system_wallet`, `below_clearing`, `below_min_bid`) has **no** quote row:
   ```bash
   sqlite3 lfg_nfts_testnet.db "SELECT session_id, state, outcome, offer_index FROM fee_cover_quotes ORDER BY created_at DESC LIMIT 3"
   ```
3. **Ops.** The broker settles the bid, as cafe's bot would:
   ```bash
   .venv/bin/python scripts/fee_cover_rehearsal.py broker-accept --state $STATE --buy-offer <offer_index>
   ```
   It reads the bid on-ledger and takes `NFTokenBrokerFee = ceil(bid × 0.01589)`.
   It refuses a bid that wouldn't clear the ask after that fee, since a real
   broker would never fill one.
4. **Ops.** Wait for the refund and audit it:
   ```bash
   .venv/bin/python scripts/fee_cover_rehearsal.py verify --offer-index <offer_index>
   ```
   `--offer-index` follows any bid, including one declined at its start.
   `--bid <session_id>` is the alternative for a bid that was quoted as
   covered.
   `verify` polls every 15 s (`--timeout`, default 1800 s) until the fee cover
   leaves `quoted`/`open`/`owed`/`submitted`. It prints the outcome, runs
   `scripts/fee_cover_report.py --network testnet --audit`, and exits 0 only on a
   `confirmed` refund with a clean audit. The staging settlement sweep settles
   and pays every 2 minutes; the bidder's own status poll may record the owed
   refund sooner.

**Pass:** `fee cover: confirmed`, a refund tx hash, and `AUDIT PASS`. On a
testnet explorer that hash is an XRP `Payment` from the issuer to the bidder,
with memo action `fee-cover` and memo `lfg:fee_cover:<accept hash>`.

**Afterwards (owner):** stop the campaign (`/admin`, or
`POST /api/admin/fee-cover/stop` with the same token and
`{"actor":"owner:t22-rehearsal"}`). Remove `BROKER_ALLOWLIST_PATH` and restart
`stg-activity`, unless another rehearsal follows. For another rehearsal, use a
fresh `--state` file: the character now belongs to the bidder.

## 4. Why a rehearsal refund gets declined

`verify --offer-index <index>` prints the reason for any of these, including
the three decided when the bid starts, which have no quote and so cannot be
reached by `--bid`. `fee_cover_report.py` counts them under
`declines by reason`.

| What happened | State · reason | Decided at |
|---|---|---|
| The seller and bidder are linked: the seller wallet is signed in to LFG under the bidder's account, signs in the same Xaman install (its push token links wallets), or has the bidder's non-exchange activation funder | refund `declined` · `linked_counterparty` | settlement |
| The character's TransferFee is 0 (`NFT_TRANSFER_FEE=0`), so no royalty reaches the issuer | refund `declined` · `royalty_unobserved` | settlement |
| The bidder is `SIGNING_ACCOUNT` (the testnet issuer). It's in `sponsored_mint.excluded_wallets()`, so the quote refuses it first | quote + promise `declined` · `system_wallet` | bid |
| The NFT's issuer is itself a party to the sale, e.g. the character was listed straight from the issuer instead of through `mint-to-seller` | refund `declined` · `issuer_party` | settlement |
| The bidder is in `SPONSORED_MINT_EXCLUDED_WALLETS`, or is the BRIX distributor | quote + promise `declined` · `system_wallet` | bid |
| The bid is below the listing's clearing price | quote + promise `declined` · `below_clearing` | bid |
| The bid is below the campaign's minimum bid | quote + promise `declined` · `below_min_bid` | bid |

A promise declined at bid time never reaches settlement: `broker-accept` still
fills the bid, but no refund is owed. Those three rows are also the ones the
bid start records no quote for, so look them up by offer index (§3 step 2).
Other ends also stop `verify` without a refund:

- `released`: the bid was cancelled, or expired unfilled once the archive had
  certified past its expiry.
- `quote_uncovered`: the campaign stopped, or the listing was gone, before the
  promise was written.
- `quote_expired`, `quote_failed`, `quote_abandoned`: the bid never validated
  (its payload expired unsigned, it was signed by another wallet or failed
  on-ledger, or it stayed unresolved past its own expiry).
- `no_quote` (`--bid` only): no covered quote exists for that session id —
  every bid declined at its start looks like this. Use `--offer-index`.
- `no_promise` (`--offer-index` only): no promise for that offer index. Either
  the bid's promise has not been written yet (the bidder's status poll writes
  it), or the bid was an ordinary one the campaign never covered.
