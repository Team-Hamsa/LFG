# Equip fail-closed branches leave the Build UI showing traits the character may no longer wear — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #316

## Problem

`lfg_core/economy_flow.run_equip` (the batched-save Build flow, #313) has two
terminal failure branches where the character may genuinely have kept its new
traits **on-ledger**, yet the client is told only `state == "failed"` with a
prose `error` string:

- **`equip_sync_indeterminate`** (`ClosetIndeterminateError`, `economy_flow.py`
  ~1035–1045): the Closet swap outcome is unknown. Fail-closed by design — no
  revert, no on-chain compensation, an admin reconciles from chain. The branch's
  own error text says "the character keeps its new traits."
- **`failed_revert`** (`economy_flow.py` ~1060–1067): the Closet swap
  definitively did not commit, so the character *should* be reverted, but the
  modify-back returned a falsy hash (or there was no decodable old URI). The
  character may still carry the new traits. Error text says "may retain the new
  traits."

Both correctly journal to `ECONOMY_RECORDS_DIR` for operator recovery, and #313
deliberately does **not** stamp `onchain_nfts` on either path (stamping an
unknown outcome is worse than not stamping).

**The gap is entirely on the read/UI side.** The service exposes an equip
session to the client through `webapp/economy_api.economy_session_dict("equip",
s)`, which returns only `{id, state, error, displaced}` — there is **no
machine-readable outcome discriminator**. In `webapp/client/app.js::saveBuild`,
`state === "failed"` throws, the `finally` block skips `applySavedLocally`
because `committed` is false, refetches `/api/economy`, and redraws the
character from the (still pre-save) index. So on these two paths the UI asserts
"your save failed, here is your old look" while the ledger may hold the *new*
look. The user's most likely next move is to re-stage and re-save the same
outfit against a character whose real on-chain state is unknown.

This is not a #313 regression (per-click equip had the same branches), but
batching raises the stakes: it is a whole outfit that appears to have failed,
not one trait.

## Constraints discovered

- **Fail-closed taxonomy is correct and must not change.** The
  `ClosetError` / `ClosetMirrorError(tx_hash)` / `ClosetIndeterminateError`
  taxonomy (`_sync_then_persist`, `economy_flow.py` module docstring rows) and
  the "no on-chain compensation against an unknown Closet" rule are load-bearing.
  This work adds a *read-side signal*; it does not add a revert or re-apply.
- **The index is not authoritative for these two branches.** `onchain_nfts` is
  intentionally left un-stamped, so refetching `/api/economy` cannot resolve the
  ambiguity — the client must be told the redraw is *uncertain*, not treat it as
  truth. Full chain-reconcile-on-read (issue option 3) is heavier and out of
  scope for the cheap fix.
- **No new on-ledger transaction.** This change is presentation + one new
  session field; nothing signs or submits, so `SourceTag = 2606160021` and
  provenance memos are not touched (they remain correct on the equip
  `NFTokenModify` itself, unchanged).
- **`EquipSession` does not currently persist the terminal journal status** — it
  is passed inline to `session._record(<status>)` and never stored on the
  dataclass. Surfacing it to the client requires a stored field.
- **Shared session dict + `kind` guard.** `_make_economy_status_handler("equip")`
  returns `session.to_dict()`; `economy_session_dict` branches on `kind`, so the
  new field is added only to the `equip` branch and cannot leak into other ops.
- **Vanilla no-build client.** Any `app.js` edit bumps the `app.js?v=` cache
  buster in `webapp/client/index.html` (currently `v=32`) in the same commit.
  Pure Build decision logic lives in `webapp/client/build_pure.js` (tested under
  Node via `tests/test_build_pure_js.py`); UI wiring stays in `app.js`.
- **`saveBusy` / `pendingEquips` discipline.** `saveBuild`'s `finally` always
  clears `pendingEquips` and drops `saveBusy`; `confirmDiscardIfDirty` and
  `renderSaveBar` gate on `saveBusy`. Any client gate added here must respect
  that same single-flight discipline.

## Design

Two independent seams: a server signal, and a client that acts on it.

### Seam 1 — server: a machine-readable `resolution` on the equip session

Add a stored field to `EquipSession` (`lfg_core/economy_flow.py`):

```python
@dataclass
class EquipSession:
    ...
    resolution: str | None = None   # None until a terminal branch classifies the outcome
```

Set it at the terminal branches of `run_equip`, classifying each failure by
whether the character's on-ledger state is **known** or **uncertain**:

| Branch (journal status)        | `resolution`   | Meaning for the UI                                  |
|--------------------------------|----------------|-----------------------------------------------------|
| success (`complete`)           | `"committed"`  | index truth is correct (client already applies save) |
| `complete_pending_mirror`      | `"committed"`  | new traits on-ledger; listener converges mirror     |
| `reverted_modify`              | `"reverted"`   | character definitively unchanged — redraw pre-save  |
| `failed_modify`                | `"reverted"`   | modify never landed — character unchanged           |
| precheck / empty / stale fail  | `"reverted"`   | ledger never touched — character unchanged          |
| **`equip_sync_indeterminate`** | `"uncertain"`  | outcome UNKNOWN — do not trust the redraw           |
| **`failed_revert`**            | `"uncertain"`  | may retain new traits — do not trust the redraw     |
| generic outer-catch (`failed`) | `None`         | conservative default → client treats as `reverted`  |

Surface it in `webapp/economy_api.economy_session_dict`, equip branch only:

```python
if kind == "equip":
    base["displaced"] = [{"slot": k, "value": v} for k, v in s.displaced.items()]
    base["resolution"] = getattr(s, "resolution", None)
```

`getattr` default keeps old journals / `mock_economy` fakes safe. No status
handler change — `_make_economy_status_handler("equip")` already returns
`to_dict()`.

The prose `error` strings stay as-is (they are already honest); `resolution` is
the *machine* discriminator the client branches on.

### Seam 2 — client: reconcile the Build UI honestly

In `webapp/client/app.js::saveBuild`, branch on `final.resolution` instead of
treating every `state === "failed"` identically:

- **`committed`** — unchanged happy path (`applySavedLocally`, refetch).
- **`reverted` / `null` / anything else** — **today's behavior**: clean-fail
  message ("Save didn't go through — your character is unchanged."), refetch
  `/api/economy`, redraw the (authoritative) pre-save character.
- **`uncertain`** — the new path:
  1. Message clearly distinguishes it: *"We couldn't confirm your save on the
     ledger. Your character may or may not be wearing the new traits — support is
     reconciling. Refresh to re-check; don't re-save until then."*
  2. Still refetch `/api/economy` (best-effort), but **do not present the redraw
     as authoritative** — set a per-character "reconcile pending" flag in
     client session state (`reconcileUncertainIds`, a `Set` of `nft_id`, mirrors
     the existing `harvestingIds` pattern) and render a persistent banner on that
     character in `renderCanvas` / the Build panel.
  3. **Gate further staging/saving on that character** while the flag is set:
     `stagePendingEquip` and `saveBuild` early-return for an `nft_id` in
     `reconcileUncertainIds` (with the message above), the same shape as the
     existing `saveBusy` guard. The flag clears when a subsequent
     `/api/economy` refetch (e.g. the user hits Refresh, or re-selects the
     character) returns — the listener/admin reconcile will by then have
     stamped the index, so the next authoritative read is trustworthy. (Clearing
     on *any* successful refetch is acceptable for the cheap version; a durable
     server-side gate is a maintainer option below.)

The indeterminate case is thus covered specifically: it is the primary producer
of `resolution == "uncertain"`, and it is exactly the case where silently
redrawing the pre-save look and inviting a re-save is most dangerous.

### Data-model changes

None. No table, no column, no migration — `resolution` is an in-memory session
field and a client-session `Set`. (The optional durable gate below would add a
column; deliberately deferred.)

## Out of scope

- **Chain-reconcile-on-read** (issue option 3): re-fetching the token's
  on-ledger URI for a character with an unresolved journal record instead of
  trusting the index. More correct, more expensive; a follow-up if the branches
  fire often.
- **Durable server-side refuse-further-equips gate** (issue option 2): a
  persistent `closet_tokens` flag akin to `mirror_pending` /
  `_mirror_pending_error`, so the *server* 4xx-refuses a new equip until the
  journal record is resolved. This spec does the equivalent *client-session*
  gate; the durable version is a maintainer decision (see below).
- Any change to the fail-closed taxonomy, the revert logic, or index-stamping
  policy.
- Harvest / assemble / extract / deposit UIs (they have their own
  indeterminate branches; this issue is scoped to equip / Build).

## Open questions / decisions for maintainer

1. **How often do these branches actually fire?** The issue explicitly says to
   check `ECONOMY_RECORDS_DIR` for real `equip_sync_indeterminate` /
   `failed_revert` occurrences before building anything. If they are effectively
   never seen in production, ship **Seam 1 + the `uncertain` message only** and
   skip the client gate/banner. Grep the journal:
   `grep -l '"status": "equip_sync_indeterminate"\|"status": "failed_revert"'
   $ECONOMY_RECORDS_DIR/equip-*.json`.
2. **Client-session gate vs durable server gate.** Is the client-session
   `reconcileUncertainIds` gate enough, or does the maintainer want the durable
   `mirror_pending`-style server refusal (issue option 2)? The former is lost on
   an Activity relaunch (Discord kills the webview on app-switch); the latter
   survives but costs a column + `read_economy_state` surfacing + a
   `_gate`-style precheck in `run_equip`.
3. **When to clear the client flag.** Clear on any successful `/api/economy`
   refetch (simple, may clear before the admin actually reconciles), or only
   once the character's on-ledger URI is confirmed (needs option 3)? Cheap
   version clears on refetch.
4. **Should the generic outer-catch default to `uncertain` instead of
   `reverted`?** It can fire after the modify commits (e.g. a post-DONE index
   stamp raising). Defaulting to `reverted` matches today's UX but could
   under-warn in a rare post-commit raise. Recommendation: keep `reverted`
   (matches current behavior; the known post-commit paths are already caught
   explicitly), revisit if audits show otherwise.

## Testing

**Unit — `tests/test_economy_flow_equip.py`** (fakes-driven, no network; the
file already exercises every branch):
- Extend `test_equip_indeterminate_no_revert` to assert
  `s.resolution == "uncertain"` alongside the existing
  `record["status"] == "equip_sync_indeterminate"`.
- Extend `test_equip_bucket_fails_and_uri_undecodable_reports_honestly` and
  `test_equip_revert_modify_not_landing_marks_failed_revert` to assert
  `s.resolution == "uncertain"`.
- Extend `test_equip_modify_then_bucket_fails_reverts` to assert
  `s.resolution == "reverted"`.
- Extend `test_equip_happy_path` and `test_equip_mirror_failure_keeps_new_traits`
  to assert `s.resolution == "committed"`.

**Unit — `webapp/economy_api`** (new small test, or fold into an existing
economy-api test): `economy_session_dict("equip", fake)` includes `resolution`,
and passing a fake without the attribute yields `resolution: None` (getattr
safety).

**Client — `tests/test_build_pure_js.py`** (Node harness): if the
resolution→UI-decision mapping is extracted into `build_pure.js` (recommended —
e.g. `buildPure.saveOutcome(resolution)` → `"committed" | "reverted" |
"uncertain"`), test the three-way classification there, including the
`null`/unknown → treated-as-`reverted` fallback.

**Manual smoke (Activity):** with a fake or a forced `raise_closet_modify`
equivalent on staging, run a batched Build save that lands on
`equip_sync_indeterminate`; confirm the UI shows the "couldn't confirm — refresh,
don't re-save" message (not "here's your old look"), the character carries the
reconcile banner, and staging/saving on it is refused until a refresh. Confirm a
clean `reverted` save still shows the old look with the plain message and no
banner.
