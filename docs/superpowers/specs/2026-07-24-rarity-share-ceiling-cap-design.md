# Share-ceiling cap in the rarity engine — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #198

## Problem

`lfg_core/rarity.py::effective_weight` weights every candidate trait as
`max(live_share, floor) × boost` per `(body, category)` with **no upper bound**.
Because the share term is proportional to the trait's *current* live population
share, an already-common trait keeps getting picked more, which raises its
share, which raises its weight — a positive-feedback runaway. This produced the
sailor-suit / Reaper-Robe / "Its Alive" over-representation.

The current mitigation (2026-07-12, mainnet only) is **manual**: the worst
offenders were hard-disabled (`enabled=0`) — 8 Clothing values, Accessory >3%
except Bible, Eyes >3%, and the season-3 green/white/grey bodies (memory
`lfg-clothing-traits-parked-off`, `scripts/disable_season_traits.py`). Manual
pruning does not scale, needs constant babysitting, and an absolute threshold
does not generalize: 3% is a runaway in a deep category like Eyes (~40 male
candidates, ~2.5% fair share) but merely *average* in a flat category like Mouth
or Eyebrows. There is no automatic governor that stops a trait before an operator
notices and parks it.

Relevant code paths:
- `effective_weight(live_count, category_total, floor_weight, boost_initial, boost_step_hours, boost_started_at, now, population_size=0)` — the pure weight function (Laplace-smoothed share when `population_size` is given).
- `weighted_pick(conn, body, category, available, ...)` — reads `trait_rarity` live on every mint, fetches candidate rows plus the whole-population `SUM(live_count)` (`total`) and `COUNT(*)` (`population`), then calls `effective_weight` per candidate and `rng.choices`.
- `get_odds(...)` and `scripts/trait_dashboard.py` — admin views that call `effective_weight` with `population_size=len(rows)` so displayed numbers match the picker.
- `lfg_core/config.py` — `RARITY_FLOOR`, `RARITY_BOOST_INITIAL`, `RARITY_BOOST_STEP_HOURS`.

## Constraints discovered

- **Purity + injectability.** `rarity.py` is pure `sqlite3` + stdlib with `now`
  and `rng` injected for tests; the cap must stay a pure function of its inputs
  (no new I/O in `effective_weight`).
- **Numbers must match across picker and admin views.** `weighted_pick`,
  `get_odds`, and `trait_dashboard.py` all funnel through `effective_weight`; the
  admin views exist *specifically* to show "the weights the picker actually
  uses". Any new argument to `effective_weight` must be threaded through all
  three call sites or the dashboard silently diverges from mint reality.
- **The hard kill switch stays.** `enabled=0` is orthogonal and unchanged; the
  cap is a soft plateau that coexists with it. Once live, the manually-parked
  traits *could* be re-enabled and left to the cap, but that is an ops decision,
  not part of this change.
- **Boost must not be fought by the cap.** A deliberately-armed rare (via
  `arm_boost`) is a curated event; the cap governs the organic share term only,
  never the `boost_multiplier` factor.
- **Laplace smoothing already exists** for cold-start bodies (milady fix,
  70281dd). The cap sits *after* the share is computed (smoothed or not) and
  *before* boost is applied — it must not undo the smoothing that stops
  brand-new bodies snowballing.
- **Backwards-compatible default.** The engine is live on mainnet; the cap must
  be a no-op until explicitly opted in (so it can be enabled mainnet-only, and so
  every existing `effective_weight` test that passes no cap keeps its exact
  numeric result).
- **No on-ledger surface.** This is mint-selection math only — no XRPL
  transaction is built, so SourceTag / provenance-memo requirements do not apply
  to this change.
- **Network-aware `trait_rarity`.** All rows carry a `network` column; the cap is
  purely per-row math, so it is automatically network-correct with no extra work.

## Design

### The clamp (adopting the issue's recommended options A + plateau)

Add an optional ceiling to `effective_weight`, computed from the number of
candidates in the actual pick — self-adapting per `(body, category)`, so flat
categories exempt themselves and only runaways are capped:

```python
def effective_weight(
    live_count, category_total, floor_weight,
    boost_initial, boost_step_hours, boost_started_at, now,
    population_size=0,
    candidate_count=0,        # NEW: # enabled candidates in this pick
    cap_multiple=0.0,         # NEW: RARITY_CAP_MULTIPLE; 0/unset = no cap
):
    if population_size:
        share = (live_count + 1) / (category_total + population_size)
    else:
        share = (live_count / category_total) if category_total else 0.0
    base = max(share, floor_weight)
    if cap_multiple and candidate_count:
        fair_share = 1.0 / candidate_count
        ceiling = max(cap_multiple * fair_share, floor_weight)  # cap never below floor
        base = min(base, ceiling)
    return base * boost_multiplier(boost_initial, boost_step_hours, boost_started_at, now)
```

Semantics (matches the issue exactly):
- **Below ceiling:** identical to today — existing proportional-with-floor
  distribution preserved.
- **At/above ceiling:** the share term stops growing (a *plateau*, not a kill).
  The trait keeps minting at ~ceiling-rate; as the category grows, its realized
  share drifts back down and self-corrects. Reversible and gentle.
- **`cap_multiple == 0` (default) or `candidate_count == 0`:** no clamp —
  byte-for-byte identical to today. This is the gate: `RARITY_CAP_MULTIPLE`
  unset/0 ⇒ the whole feature is off.
- **`ceiling = max(cap_multiple × fair_share, floor_weight)`** guarantees the
  ceiling can never sink below a trait's own floor (a floor is a deliberate
  minimum; the cap must not fight it).

### Config knob

`lfg_core/config.py`, alongside the other rarity knobs:

```python
RARITY_CAP_MULTIPLE = float(os.getenv("RARITY_CAP_MULTIPLE", "0"))  # 0/unset = no share ceiling
```

Default `0` ⇒ off everywhere until opted in (e.g. mainnet-only via the mainnet
stack's `.env`). Proposed live value `3.0`: for male Eyes (~40 candidates, fair
2.5%) the ceiling is 7.5%, which catches only Its Alive / No Sleep / Hypno and
leaves flat categories (Mouth, Eyebrows) untouched.

### Candidate count — where it comes from

- **`weighted_pick`** already has the exact enabled-candidate set: `rows` is the
  list of `enabled=1` `trait_rarity` rows whose `trait IN (available)`. It passes
  `candidate_count=len(rows)` and `cap_multiple=config.RARITY_CAP_MULTIPLE` to
  every `effective_weight` call. This is the issue's ratified grain: `N` = number
  of *enabled* candidates in the real pick, not the raw population row count that
  feeds Laplace smoothing (so disabled / `None` placeholders never distort fair
  share).
- **`get_odds` / `trait_dashboard.py`** don't have the layer-store `available`
  list, so they approximate `candidate_count` with the number of **enabled** rows
  in the `(body, category)` group. This is a close upper bound on the true pick
  candidate set and keeps the admin ceiling display honest. Both pass
  `cap_multiple=config.RARITY_CAP_MULTIPLE`.

### Admin visibility

The dashboard (`scripts/trait_dashboard.py`) and `get_odds` already surface
share and effective weight. With the cap threaded through, the displayed
`weight` automatically reflects the plateau, so an operator sees a capped trait's
weight stop rising. Additionally, expose the ceiling itself so the UI can flag
capped traits:
- `get_odds` gains a per-row `ceiling` (or a `capped: bool`) in its tuple, or —
  to avoid churning its public 5-tuple — the dashboard computes the ceiling
  inline (`RARITY_CAP_MULTIPLE / enabled_candidate_count` per group) and adds a
  `"capped": weight_at_ceiling` / `"ceiling"` field to each `/api/traits` row,
  rendering a "capped" badge in grid/list view. (Chosen shape is an open
  question below — the dashboard-only field avoids breaking `get_odds`
  consumers.)

No new table, no migration, no on-chain effect. The cap governs **future picks
only** — existing NFTs are never re-balanced.

## Out of scope

- Per-trait cap exemptions or a DB-column override table (YAGNI — the issue
  defers these).
- Retroactive re-balancing of already-minted NFTs.
- Absolute-percent ceilings and per-category ceiling overrides (options B/C —
  rejected in favor of the self-adapting fair-share basis).
- Auto-*disabling* a runaway (flip `enabled=0`) — the design plateaus, it does
  not kill; the hard kill switch stays a manual operator action.
- Any change to `boost` mechanics, Laplace smoothing, or the `enabled` flag.

## Open questions / decisions for maintainer

1. **Ratify option A (fair-share basis) + plateau behavior + default multiple
   3.0 + `RARITY_CAP_MULTIPLE=0` gate.** The issue proposes these as recommended
   but explicitly parked them pending @joshuahamsa's sign-off. This spec builds
   on the recommendation; confirm before implementation.
2. **Hysteresis / taper.** The plateau is inherently hysteresis-free (weight
   simply stops growing; no oscillation, because the clamp is a `min`, not a
   state machine). The issue's alternative — a harder "taper toward floor once
   over ceiling" — *would* need hysteresis (a band so a trait doesn't flap on/off
   around the threshold). Do we want the gentle plateau (recommended, no
   hysteresis needed) or the aggressive taper (needs a lower re-arm band)?
3. **Admin-visibility shape.** Add `ceiling`/`capped` to `get_odds`' tuple (touches
   Discord `/admin` + CLI consumers) or keep it dashboard-only via a computed
   `/api/traits` field (no public-API churn)? Recommend dashboard-only.
4. **`candidate_count` for admin views.** Confirm the enabled-row-count
   approximation is acceptable for `get_odds`/dashboard display (the true pick
   candidate set depends on the layer-store `available` list the admin views
   don't have). It only affects the *displayed* ceiling, never the picker's.
5. **Should the go-live also re-enable the manually-parked traits** so the cap
   governs them, or leave them disabled and let the cap prevent *future*
   runaways only? (Ops decision, out of this PR's code but worth deciding.)

## Testing

Unit (`tests/test_rarity.py`, existing env-guard preamble already present):
- **Below ceiling unchanged:** `effective_weight(..., candidate_count=40, cap_multiple=3.0)` with a share under 7.5% returns the same value as with `cap_multiple=0`.
- **At/above ceiling clamped:** a trait whose smoothed share is 20% with `candidate_count=40, cap_multiple=3.0` clamps to `0.075 × boost`.
- **Ceiling never below floor:** with a tiny `candidate_count` making `cap_multiple × fair_share < floor_weight`, `base` clamps to `floor_weight`, not the sub-floor ceiling.
- **`cap_multiple=0` disables:** result identical to a call omitting the cap args (regression guard for the gate; every existing `effective_weight` test still passes untouched).
- **`candidate_count=0` disables** even with a non-zero multiple.
- **Boost untouched by cap:** a capped share still multiplies by the full `boost_multiplier` (cap applies to `base` only).

Integration:
- **`weighted_pick` bounds a runaway:** seed a `(body, category)` where one trait holds a dominant share; with `RARITY_CAP_MULTIPLE` monkeypatched to 3.0, assert over many seeded `rng.choices` draws that the runaway's pick probability is bounded near ceiling/Σweights, materially below the uncapped case (mirror the style of `test_weighted_pick_respects_weights` / `test_weighted_pick_denominator_spans_whole_category`).
- **Cap off ⇒ picker unchanged:** with `RARITY_CAP_MULTIPLE=0`, `weighted_pick` reproduces today's distribution.

Manual smoke:
- Run `scripts/trait_dashboard.py --network mainnet` with `RARITY_CAP_MULTIPLE=3.0` in env; confirm a known over-represented trait (e.g. a high-share Eyes value) shows a capped weight / badge and that flat-category traits are unaffected.
- Confirm a live testnet mint still succeeds with the cap on (no empty-category regression).
