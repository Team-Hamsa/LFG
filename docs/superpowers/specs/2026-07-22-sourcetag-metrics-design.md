# SourceTag metrics — living README badge

**Date:** 2026-07-22
**Status:** Design approved, pending implementation plan

## Problem

The XRPL Make Waves Hackathon scores transaction volume only for transactions
carrying our assigned `SourceTag = 2606160021`. We have no way to see that
number, nor how many distinct wallets have actually used the project. The data
already exists — `lfg_core/history_store.py` persists a `source_tag` column on
every archived transaction, and the pm2 listeners dual-write it live — but
nothing reads it.

Goal: a self-updating SVG badge in the README showing tagged-transaction volume
and unique participating wallets, in the existing brand style.

## Baseline (measured 2026-07-22, `history_mainnet.db`)

- 1,943 tagged transactions (grows continuously; the listener is live)
- 19 distinct signing accounts → **16** after excluding our own wallets
- By type: Mint 700 · CreateOffer 692 · AcceptOffer 311 · Modify 89 · Burn 77 ·
  Payment 64 · CancelOffer 2

`close_time` in `xrpl_txs` is stored as **unix** seconds, not the ripple epoch.
Any query that applies the usual `- 946684800` correction is wrong and silently
widens the window. Implementations must not apply it.

## Scope decisions

**No date filter.** The tag is recent enough that all-time and
since-2026-07-10 differ by 22 transactions. Reporting all-time is simpler and
loses nothing.

**Unique wallets counts signers, not recipients.** A wallet is counted when it
*signed* a tagged transaction. Recipients are not counted and are not
reported. Measured for the record: 24 distinct `Destination` addresses vs 19
signers, the gap being two infrastructure addresses (`rLfgoBriX…`, our BRIX
issuer; `rBETMo1JS…`) and three wallets that were offered an NFT and never
accepted it. A wallet that ignored an offer did not interact with the project.

**The exclusion set applies to `unique_wallets` only, not to
`total_tagged_txs`.** This asymmetry is deliberate and must not be
"corrected". `unique_wallets` answers "how many people used this", so our own
wallets are removed. `total_tagged_txs` answers "how much tagged volume did
this project generate", and an issuer-signed `NFTokenMint` is our volume
regardless of who pressed the button — the hackathon scores it. The two
figures are measured over the same rows but filtered differently: 16 wallets,
1,943 transactions. Filtering the transaction count the same way yields 280
(Accept 262 · Pay 17 · Offer 1), because the backend signs every mint, offer,
modify and burn and a user's only signature is the accept or the payment.
That view discards roughly 85% of the tagged volume and is not what the badge
reports.

**Exclusion set.** `rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ`,
`rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf` (the operator's wallets) and
`rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ` (the backend-signing issuer, 1,590 txs).
Excluded addresses are listed in the JSON output so the number is auditable.

## Architecture

Three components, split because the data lives somewhere CI cannot reach.
`history_mainnet.db` is gitignored and exists only on the deploy box, so
GitHub Actions cannot compute these numbers the way `hackathon_loc.py` and
`readme_dashboard.py` compute theirs from git.

```
deploy box (pm2 cron)              GitHub                    README
─────────────────────              ──────                    ──────
sourcetag_metrics.py  ──gh api──>  metrics/sourcetag.json
                                            │
                                            │ push to main
                                            v
                                   hackathon-loc.yml
                                   render_sourcetag_svg.py
                                            │
                                            v
                                   assets/sourcetag.svg  ──>  badge
```

### 1. Collector — `scripts/sourcetag_metrics.py`

A read-only pass over `history_<network>.db`. No chain calls, no writes to any
other store.

Flags: `--network mainnet|testnet` (default from `config.XRPL_NETWORK`),
`--json`, `--out PATH` (default `metrics/sourcetag.json`), `--push`.

Output schema:

| field | type | meaning |
|---|---|---|
| `source_tag` | int | 2606160021, echoed for provenance |
| `network` | str | `mainnet` |
| `total_tagged_txs` | int | all rows with `source_tag = 2606160021` |
| `unique_wallets` | int | distinct `account`, minus the exclusion set |
| `by_type` | dict | tx type → count, descending |
| `daily` | list | `[{date, count}]`, gap-filled contiguous UTC days |
| `excluded` | list | the excluded addresses, explicitly |
| `first_tagged_tx` | str | ISO date of the earliest tagged transaction |
| `archive_max_close_time` | str | ISO timestamp of the newest archived tx — freshness |
| `as_of` | str | ISO timestamp of this run |

The exclusion set is a module-level constant with the issuer resolved from
config rather than hardcoded, so a key rotation does not silently start
counting the backend as a user.

`--push` writes nothing into any working tree. It base64-encodes the JSON and
issues a single `gh api -X PUT
repos/Team-Hamsa/LFG/contents/metrics/sourcetag.json` against `main`, supplying
the existing blob `sha`. This deliberately avoids a local checkout: `~/LFG`
stays clean on `deploy` and `~/LFG-staging` stays clean on `main`, so neither
polling deployer can observe divergence and halt. `gh` is already authenticated
on the box.

The push is skipped when the computed JSON is byte-identical to what is already
on `main` ignoring `as_of`, so quiet days produce no commit and no CI run.

### 2. Publishing — pm2 cron

```bash
pm2 start scripts/sourcetag_metrics.py --name lfg-sourcetag \
  --cron "20 0 * * *" --no-autorestart --interpreter .venv/bin/python \
  -- --network mainnet --push
```

00:20 UTC, after `lfg-snapshot` at 00:10. `--no-autorestart` means pm2 shows the
process "stopped" between runs; that is normal, matching `lfg-snapshot`.

Requires adding `metrics/**` to `ci.yml`'s `paths-ignore`. Without it every
daily JSON commit drags the full ruff/mypy/gitleaks/pytest gate through CI for
a file no code imports.

### 3. Renderer — `scripts/render_sourcetag_svg.py`

Reads `metrics/sourcetag.json`, writes `assets/sourcetag.svg`. Touches no
database, so it runs on a CI runner. Idempotent: the file is rewritten only
when its content changes.

Wired as a third step in `.github/workflows/hackathon-loc.yml`, with
`assets/sourcetag.svg` added to that job's `git add` line. The workflow's
existing `github.actor != 'github-actions[bot]'` guard prevents a self-trigger
loop; the box pushes as `joshuahamsa`, so a JSON commit does trigger a render.
No `[skip ci]` is added, for the reason already documented in that workflow.

**Branding.** Reuses `readme_dashboard.py`'s constants verbatim — `INK`,
`SURFACE`, `SURFACE_LIGHT`, `LINE`, `PAPER`, `TEXT`, `MUTED`, and the accent
set `ORANGE`/`RED`/`BLUE`/`YELLOW`/`GREEN`/`PURPLE`, plus the system `FONT`
stack. Same sticker construction: an 8px hard `INK` offset shadow, a `SURFACE`
card with a 3px `PAPER` ring so it reads on both GitHub light and dark themes,
`SURFACE_LIGHT` tiles with 1px `LINE` hairlines, large brand-colored numerals
over `MUTED` lowercase labels. Width is 728px to match `dashboard.svg` so the
badges stack flush.

Layout:

```
┌──────────────────────────────────────────────────────────┐
│  XRPL source tag · 2606160021                            │
│  live on-ledger volume · auto-updated daily              │
│                                                          │
│    16                    1,943                           │
│    unique wallets        tagged transactions             │
│                                                          │
│  mint    ████████████████████ 700                        │
│  offer   ███████████████████  692                        │
│  accept  ████████             311                        │
│  modify  ██ 89   burn ██ 77   pay █ 64                   │
│                                                          │
│  ▁▂▅▃▇▄▂▆█▃▂▁▄▇▅▂▃   tagged tx per day                   │
└──────────────────────────────────────────────────────────┘
```

Two stat tiles, a horizontal bar group for the type breakdown, and a
sparkline strip. The `daily` series is gap-filled so a quiet stretch reads as
zero-height bars, matching `readme_dashboard.velocity()`.

A `role="img"` + `aria-label` describing the figures, as the existing badges do.

## Error handling

- Database missing or unreadable → exit non-zero, no push, no partial write.
  The previous JSON and SVG stay in place rather than being replaced with
  something wrong.
- `gh api` failure (auth, network, sha conflict) → logged, exit non-zero. pm2
  records the failed run; the next night retries.
- `metrics/sourcetag.json` missing or malformed at render time → the renderer
  exits non-zero and the workflow fails loudly rather than emitting an empty
  badge.
- Zero tagged transactions → renders a valid badge with zeros, not a crash.

## Testing

- Collector: fixture `history_*.db` built in a tmpdir with known tagged and
  untagged rows; assert `total_tagged_txs`, `unique_wallets` honours the
  exclusion set while `total_tagged_txs` does not, `by_type` ordering, and
  `daily` gap-filling across a missing day.
- Regression: assert `close_time` is interpreted as unix seconds — a row at a
  known timestamp must land on the expected UTC date.
- `--push` is exercised with the `gh` invocation stubbed; assert no writes land
  in the working tree and that an unchanged payload (modulo `as_of`) is a
  no-op.
- Renderer: golden-ish check that the output parses as XML, contains the
  expected numerals, and uses only the brand palette constants.

## Out of scope

- Testnet badge. The collector accepts `--network testnet` for ad-hoc runs, but
  only mainnet is published.
- Backfilling tagged transactions that predate the archive. The listener's
  coverage is the definition of the metric.
- Counting transactions that carry the tag but touch none of our accounts and
  none of our NFTs. Such a transaction would not be archived; none is known to
  exist.
