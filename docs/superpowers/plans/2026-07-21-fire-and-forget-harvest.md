# Fire-and-Forget Stacked Harvests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harvest becomes fire-and-forget in the Activity — users can stack consecutive harvests and keep using the app (e.g. mint) while harvests run; a toast reports completion instead of a forced panel switch.

**Architecture:** Three independent seams. (1) `lfg_service/app.py::_economy_post` gets a per-kind conflict policy so harvests dedupe per `(user, nft_id)` instead of the blanket per-user 409. (2) `lfg_core/xrpl_ops.py::_submit_and_confirm` gains a per-signing-account lock (reusing the loop-keyed `owner_lock` registry) so concurrent backend-signed txs can't collide on account sequence. (3) `webapp/client/app.js::harvestActive` stops awaiting the poll; a background tracker polls, toasts on completion, and re-renders only if the dress-up panel is visible. The per-owner Closet lock already exists (`economy_flow._serialize_by_owner`, #180) — stacked harvests queue on it automatically; we only add a regression test.

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; vanilla no-build JS client.

## Global Constraints

- All XRPL txs keep `SourceTag = 2606160021` and provenance memos (no tx-building changes in this plan — do not touch tx dicts).
- Pre-push gate: ruff, ruff-format, mypy, gitleaks, pytest, validate-trait-config all must pass. Never `--no-verify` on the final push.
- Client is no-build vanilla JS; any `app.js` change requires bumping the cache-buster in `webapp/client/index.html` (`app.js?v=24` → `?v=25`) in the same commit.
- Error copy style matches existing strings: lowercase sentence fragments, e.g. `"an economy action is already in progress"`.

---

### Task 1: Per-kind economy conflict policy (server)

**Files:**
- Modify: `lfg_service/app.py` (the `_economy_post` factory, ~line 5098; add `_economy_conflict` just above it)
- Test: `tests/test_economy_conflict_policy.py` (new)

**Interfaces:**
- Produces: `_economy_conflict(sessions: dict, terminal: set[str], kind: str, user_id: str, platform: str, body: dict) -> str | None` — returns the 409 error string or None. `_economy_post` calls it in place of today's `_active_session` gate.
- Consumes: existing `economy_sessions` dict of `EconomyWebSession` (attrs: `discord_id`, `state`, `platform`, `kind`, `inner`; harvest inner has `.character.nft_id`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_economy_conflict_policy.py`:

```python
"""Per-kind economy 409 policy (fire-and-forget harvests spec 2026-07-21).

Harvests stack per user (409 only on the same nft_id); every other economy op
keeps per-user exclusivity and is mutually exclusive with in-flight harvests.
"""

import os
from types import SimpleNamespace

os.environ.setdefault("BUNNY_PULL_ZONE", "example.b-cdn.net")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_service.app import _economy_conflict  # noqa: E402

TERMINAL = {"done", "failed"}


def _sess(kind, nft_id=None, user="u1", platform="discord", state="running"):
    inner = SimpleNamespace(character=SimpleNamespace(nft_id=nft_id)) if nft_id else SimpleNamespace()
    return SimpleNamespace(discord_id=user, platform=platform, kind=kind, state=state, inner=inner)


def test_no_sessions_allows_everything():
    for kind in ("harvest", "equip", "assemble", "extract", "deposit"):
        assert _economy_conflict({}, TERMINAL, kind, "u1", "discord", {}) is None


def test_harvests_stack_on_different_nfts():
    sessions = {"a": _sess("harvest", nft_id="AAA")}
    assert _economy_conflict(sessions, TERMINAL, "harvest", "u1", "discord", {"nft_id": "BBB"}) is None


def test_same_nft_harvest_409s():
    sessions = {"a": _sess("harvest", nft_id="AAA")}
    err = _economy_conflict(sessions, TERMINAL, "harvest", "u1", "discord", {"nft_id": "AAA"})
    assert err == "that character is already being harvested"


def test_terminal_harvest_does_not_block():
    sessions = {"a": _sess("harvest", nft_id="AAA", state="done")}
    assert _economy_conflict(sessions, TERMINAL, "harvest", "u1", "discord", {"nft_id": "AAA"}) is None


def test_other_users_and_platforms_do_not_block():
    sessions = {
        "a": _sess("harvest", nft_id="AAA", user="u2"),
        "b": _sess("equip", user="u1", platform="telegram"),
    }
    assert _economy_conflict(sessions, TERMINAL, "harvest", "u1", "discord", {"nft_id": "AAA"}) is None
    assert _economy_conflict(sessions, TERMINAL, "equip", "u1", "discord", {}) is None


def test_non_harvest_blocks_non_harvest():
    sessions = {"a": _sess("equip")}
    err = _economy_conflict(sessions, TERMINAL, "assemble", "u1", "discord", {})
    assert err == "an economy action is already in progress"


def test_non_harvest_blocks_harvest():
    sessions = {"a": _sess("equip")}
    err = _economy_conflict(sessions, TERMINAL, "harvest", "u1", "discord", {"nft_id": "AAA"})
    assert err == "an economy action is already in progress"


def test_harvest_blocks_non_harvest():
    sessions = {"a": _sess("harvest", nft_id="AAA")}
    err = _economy_conflict(sessions, TERMINAL, "equip", "u1", "discord", {})
    assert err == "wait for your running harvests to finish first"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_economy_conflict_policy.py -v`
Expected: FAIL / ERROR with `ImportError: cannot import name '_economy_conflict'`

- [ ] **Step 3: Implement `_economy_conflict` and wire it into `_economy_post`**

In `lfg_service/app.py`, add above `_economy_post` (~line 5098):

```python
def _economy_conflict(
    sessions: dict[str, Any],
    terminal: set[str],
    kind: str,
    user_id: str,
    platform: str,
    body: dict[str, Any],
) -> str | None:
    """Per-kind concurrency policy for the economy POSTs (fire-and-forget
    harvests, spec 2026-07-21). Harvests stack per user — the only harvest
    conflict is the SAME nft_id already in flight. Every other op keeps the
    old per-user exclusivity among themselves AND is mutually exclusive with
    in-flight harvests (both share the owner's Closet; the per-owner lock in
    economy_flow makes interleaving safe but the queueing UX would be
    confusing for signature-bearing ops). Returns the 409 message or None."""
    active = [
        s
        for s in sessions.values()
        if s.discord_id == user_id
        and s.state not in terminal
        and getattr(s, "platform", "discord") == platform
    ]
    harvests = [s for s in active if getattr(s, "kind", None) == "harvest"]
    others = [s for s in active if getattr(s, "kind", None) != "harvest"]
    if others:
        return "an economy action is already in progress"
    if kind == "harvest":
        nft_id = body.get("nft_id")
        for s in harvests:
            if getattr(getattr(s.inner, "character", None), "nft_id", None) == nft_id:
                return "that character is already being harvested"
        return None
    if harvests:
        return "wait for your running harvests to finish first"
    return None
```

Then in `_economy_post`'s handler replace the gate:

```python
        _prune_sessions(economy_sessions, economy_api.TERMINAL_STATES)
        conflict = _economy_conflict(
            economy_sessions, economy_api.TERMINAL_STATES, kind, user["id"], _platform(user), body
        )
        if conflict:
            return web.json_response({"error": conflict}, status=409)
```

(Delete the old `if _active_session(...)` block. `_active_session` itself stays — the mint/swap/market paths still use it. Note `body = await request.json()` already runs before the gate, so `body` is available.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_economy_conflict_policy.py -v`
Expected: 8 PASS

- [ ] **Step 5: Run the wider service/economy suites to catch regressions**

Run: `.venv/bin/python -m pytest tests -k "economy or service" -q && .venv/bin/python -m pytest webapp -q`
Expected: all pass (no existing test asserts the old blanket 409 for stacked harvests; if one does, update it to the new policy)

- [ ] **Step 6: Commit**

```bash
git add tests/test_economy_conflict_policy.py lfg_service/app.py
git commit -m "feat(economy): per-kind 409 policy — harvests stack per nft_id"
```

---

### Task 2: Per-signing-account XRPL submit lock

**Files:**
- Modify: `lfg_core/xrpl_ops.py` (`_submit_and_confirm`, ~line 109)
- Test: `tests/test_xrpl_submit_lock.py` (new)

**Interfaces:**
- Consumes: `lfg_core.owner_lock.owner_lock(key: str) -> asyncio.Lock` (loop-keyed registry, #180).
- Produces: no signature change — `_submit_and_confirm(tx, wallet, client, label)` behaves identically but serializes per `wallet.classic_address`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_xrpl_submit_lock.py`:

```python
"""_submit_and_confirm must serialize per signing account: concurrent
backend-signed txs otherwise autofill the same sequence and one dies
tefPAST_SEQ (fire-and-forget harvests spec 2026-07-21)."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("BUNNY_PULL_ZONE", "example.b-cdn.net")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_core import xrpl_ops  # noqa: E402


def test_concurrent_submits_serialize_per_account():
    order: list[str] = []

    def fake_autofill_and_sign(tx, client, wallet):
        order.append(f"sign:{tx}")
        return SimpleNamespace(get_hash=lambda: f"H{tx}")

    def fake_submit_and_wait(signed, client, wallet, autofill):
        order.append(f"submit:{signed.get_hash()}")
        return SimpleNamespace(
            result={"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}}
        )

    wallet = SimpleNamespace(classic_address="rISSUER")

    async def go():
        with (
            patch.object(xrpl_ops, "autofill_and_sign", fake_autofill_and_sign),
            patch.object(xrpl_ops, "submit_and_wait", fake_submit_and_wait),
        ):
            await asyncio.gather(
                xrpl_ops._submit_and_confirm("tx1", wallet, None, "a"),
                xrpl_ops._submit_and_confirm("tx2", wallet, None, "b"),
            )

    asyncio.run(go())
    # Serialized: each tx's sign is immediately followed by its own submit —
    # never sign,sign,submit,submit (the sequence-collision interleaving).
    assert order in (
        ["sign:tx1", "submit:Htx1", "sign:tx2", "submit:Htx2"],
        ["sign:tx2", "submit:Htx2", "sign:tx1", "submit:Htx1"],
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_xrpl_submit_lock.py -v`
Expected: FAIL — `asyncio.to_thread` lets the two sign calls interleave, so `order` is sign,sign,submit,submit. (If it flakes to a passing order, the implementation step still stands; make the fakes `await asyncio.sleep(0)` after appending to force the interleave.)

- [ ] **Step 3: Implement the lock**

In `lfg_core/xrpl_ops.py`, import the registry (top of file, alongside the other `lfg_core` imports):

```python
from lfg_core import owner_lock
```

Wrap the body of `_submit_and_confirm` (the existing code is unchanged inside the `async with`):

```python
async def _submit_and_confirm(
    tx: Transaction, wallet: Wallet, client: JsonRpcClient, label: str
) -> dict[str, Any] | None:
    """... (keep the existing docstring, and append:)

    Serialized per signing account (fire-and-forget harvests, 2026-07-21):
    autofill reads the account sequence, so two concurrent backend-signed txs
    would sign the same sequence and one would fail tefPAST_SEQ. The lock
    spans sign→validate; backend txs pipeline instead of colliding. Keyed on
    the classic address via the loop-keyed owner_lock registry (#180)."""
    async with owner_lock.owner_lock(f"xrpl-submit:{wallet.classic_address}"):
        signed = await asyncio.to_thread(autofill_and_sign, tx, client, wallet)
        try:
            response = await asyncio.to_thread(submit_and_wait, signed, client, None, autofill=False)
        except Exception as e:
            logging.warning(f"{label}: submit_and_wait raised ({e}); confirming by hash")
            confirmed = await _confirm_by_hash(client, signed.get_hash())
            if confirmed is None:
                raise IndeterminateResultError(
                    f"{label}: on-ledger outcome unknown after submit raised ({e})"
                ) from e
            return _validated_result(confirmed, label)
        return _validated_result(response.result, label)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_xrpl_submit_lock.py -v`
Expected: PASS

- [ ] **Step 5: Run the xrpl/economy suites**

Run: `.venv/bin/python -m pytest tests -k "xrpl or economy" -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add tests/test_xrpl_submit_lock.py lfg_core/xrpl_ops.py
git commit -m "fix(xrpl): serialize backend submits per signing account"
```

---

### Task 3: Concurrent-harvest regression test (per-owner lock)

**Files:**
- Test: `tests/test_economy_flow_harvest.py` (append one test; follow the file's existing fixture/deps helpers — read the top of the file and reuse its `EconomyDeps` construction pattern rather than inventing a new one)

**Interfaces:**
- Consumes: `economy_flow.run_harvest`, `economy_flow.HarvestSession`, the file's existing fake-deps builder and seeded genesis/closet fixtures.

- [ ] **Step 1: Write the test**

Append (adapting fixture names to the file's existing helpers — the assertions are the contract):

```python
def test_two_concurrent_harvests_both_land(tmp_path):
    """#180's per-owner lock must make stacked harvests (fire-and-forget spec
    2026-07-21) serialize: run two run_harvest coroutines concurrently for one
    owner and assert BOTH characters' assets are in the closet afterwards and
    both sessions end DONE."""
    conn, deps, owner, char_a, char_b = _two_character_setup(tmp_path)  # build with the file's helpers
    sa = economy_flow.HarvestSession(owner=owner, character=char_a, burnable=True)
    sb = economy_flow.HarvestSession(owner=owner, character=char_b, burnable=True)

    async def go():
        await asyncio.gather(
            economy_flow.run_harvest(sa, deps), economy_flow.run_harvest(sb, deps)
        )

    asyncio.run(go())
    assert sa.state == economy_flow.DONE and sb.state == economy_flow.DONE
    assets = {(s, v): n for o, s, v, n in economy_store.read_closet_assets(conn) if o == owner}
    for char in (char_a, char_b):
        for attr in char.attributes:
            if attr["trait_type"] != "Body" and attr["value"] != "None":
                assert assets.get((attr["trait_type"], attr["value"]), 0) >= 1
```

If the file has no two-character helper, build `_two_character_setup` from its single-character fixture by inserting a second index character with distinct trait values.

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_economy_flow_harvest.py -v`
Expected: all PASS (the lock already exists; this is a pin-down regression test — if it fails, STOP and investigate before proceeding)

- [ ] **Step 3: Commit**

```bash
git add tests/test_economy_flow_harvest.py
git commit -m "test(economy): pin concurrent same-owner harvests both landing"
```

---

### Task 4: Client fire-and-forget harvest

**Files:**
- Modify: `webapp/client/app.js` (`harvestActive` ~line 2368; `openDressup`/`selectCharacter` area for pending-tile state)
- Modify: `webapp/client/index.html` (cache-buster `app.js?v=24` → `?v=25`)

**Interfaces:**
- Consumes: existing `api()`, `pollEconomyOp(kind, startResp)`, `toast(msg)`, `showError(msg)`, `confirmDialog(opts)`, `economyState`, `activeNftId`, `buildPure.pickDefaultCharacter`, `renderCloset()`, `el()`.
- Produces: module-level `harvestingIds` (Set of nft_id currently harvesting) — nothing else reads it yet, but keep the name stable.

- [ ] **Step 1: Rewrite `harvestActive` and add the background tracker**

Replace the whole `harvestActive` function with:

```javascript
// nft_ids with a harvest in flight (fire-and-forget, spec 2026-07-21). Used to
// keep a burned-in-progress character out of the selectable set on re-render.
const harvestingIds = new Set();

async function harvestActive() {
  const char = activeChar();
  if (!char) return;
  if (!(await confirmDialog({
    title: 'Harvest this character?',
    text: `This permanently burns #${char.edition}. Its parts go to your Closet.`,
    confirmLabel: '🔥 Harvest',
  }))) return;
  let res;
  try {
    res = await api('/api/harvest', {
      method: 'POST', body: JSON.stringify({ nft_id: char.nft_id }),
    });
  } catch (e) {
    showError(e.message);
    return;
  }
  // Fire-and-forget: drop the character from the local roster immediately so
  // the user can select + harvest the next one; the tracker below reconciles
  // real state when the op lands. Never navigate the user anywhere.
  harvestingIds.add(char.nft_id);
  economyState.characters = economyState.characters.filter((c) => c.nft_id !== char.nft_id);
  toast(`🔥 Harvesting #${char.edition} — keep playing, this takes a moment.`);
  if (activeNftId === char.nft_id) {
    activeNftId = buildPure.pickDefaultCharacter(economyState.characters);
    if (!el('dressup-panel').hidden) {
      if (activeNftId) selectCharacter(activeNftId);
      else { el('dressup-canvas').replaceChildren(); renderCloset(); }
    }
  }
  trackHarvest(char, res);
}

async function trackHarvest(char, startResp) {
  const final = await pollEconomyOp('harvest', startResp);
  harvestingIds.delete(char.nft_id);
  if (final.state === 'failed') {
    showError(`Harvest of #${char.edition} failed: ${final.error || 'unknown error'}`);
  } else {
    toast(`✅ #${char.edition} harvested — parts added to your Closet.`);
  }
  // Reconcile real state silently; re-render ONLY if the Dressing Room is the
  // visible panel — never yank the user out of another flow (e.g. a mint).
  try {
    economyState = await api('/api/economy');
  } catch (e) {
    return; // transient; next openDressup() refetches anyway
  }
  economyState.characters = economyState.characters.filter((c) => !harvestingIds.has(c.nft_id));
  if (el('dressup-panel').hidden) return;
  if (!economyState.characters.find((c) => c.nft_id === activeNftId)) {
    activeNftId = buildPure.pickDefaultCharacter(economyState.characters);
  }
  if (activeNftId) selectCharacter(activeNftId);
  else { el('dressup-canvas').replaceChildren(); renderCloset(); }
}
```

Also in `openDressup()` (Closet-active branch, ~line 2123), filter in-flight harvests out of the freshly fetched roster so a mid-harvest refresh can't resurrect the tile:

```javascript
    economyState.characters = economyState.characters.filter((c) => !harvestingIds.has(c.nft_id));
    goAssembleEnabled = true;
    activeNftId = buildPure.pickDefaultCharacter(economyState.characters);
```

- [ ] **Step 2: Bump the cache-buster**

In `webapp/client/index.html` change:

```html
<script type="module" src="app.js?v=25"></script>
```

- [ ] **Step 3: Syntax-check + smoke**

Run: `node --check webapp/client/app.js && .venv/bin/python -m pytest webapp -q`
Expected: no syntax error; webapp suite passes.

- [ ] **Step 4: Manual smoke (dev mode)**

Run: `WEBAPP_DEV_MODE=1 .venv/bin/python -m webapp.server` and in the mock client verify: harvest returns instantly with the "Harvesting…" toast, a second harvest can start immediately, navigating to Mint is never interrupted, completion toast appears, Closet shows both characters' parts after both land. (Mock economy resolves ops instantly — the ordering assertions still hold.)

- [ ] **Step 5: Commit**

```bash
git add webapp/client/app.js webapp/client/index.html
git commit -m "feat(activity): fire-and-forget stacked harvests — no panel yank"
```

---

### Task 5: Full gate + PR

- [ ] **Step 1: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 2: Lint/format/types**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .`
Expected: clean (fix anything it flags).

- [ ] **Step 3: Push branch and open PR**

```bash
git push -u origin claude/harvesting-mechanism-perf-a484ee
gh pr create --repo Team-Hamsa/LFG --title "Fire-and-forget stacked harvests" --body "..."
```

PR body: summarize the three seams + link the spec/plan docs. Per repo rules: no AI attribution, non-draft, wait for Greptile + CodeRabbit and resolve findings before merge.
