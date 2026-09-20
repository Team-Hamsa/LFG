# Mine Tab Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Marketplace **Mine** tab's nine flat chip strips with five intent-named, collapsible groups rendered as cards or rows, bounded in height by per-group caps with a searchable "Show all" drill-in.

**Architecture:** All partition / merge / sort / cap logic moves into a new DOM-free `webapp/client/mine_pure.js` (`buildMine()`), Node-tested exactly like `market_pure` and `closet_market_pure`. `app.js` keeps only DOM glue: `renderMine()` walks `buildMine()`'s output and emits `.nft-card`s (Selling, Your stuff) or `.mine-row`s (Needs you, Buying, History), dispatching each item's `action.kind` through a handler table. **No server change** — the three existing endpoints are consumed unchanged.

**Tech Stack:** Vanilla ES modules (no build step), aiohttp-served static client, pytest + Node for the pure-module tests.

**Spec:** `docs/superpowers/specs/2026-09-20-mine-tab-redesign-design.md` — read it before Task 1; this plan argues from it.

## Global Constraints

- **No server change.** `/api/market/mine`, `/api/market/bids/mine` and `/api/closet/orders/mine` keep their current shape and are not edited. No new endpoint, no `LIMIT` added server-side.
- **Cache busters move in lockstep, in the same commit:** `style.v42.css` → `style.v43.css` (rename the file, update `index.html`), `app.js?v=110` → `?v=111` in `index.html`, and the new import `./mine_pure.js?v=1` in `app.js`. `tests/test_app_js_deeplink.py:86` pins `"app.js?v=110"` exactly — it must be updated in the same commit or the gate fails.
- **`renderChipList` is NOT deleted.** `loadClosetBook` (`closet-book-bids`, the Wanted tab) and the Dressing Room trait strip still call it. Only Mine stops calling it.
- **Group order on screen:** Needs you, Selling, Buying, Your stuff, History.
- **Inline caps:** Needs you 20, Selling 12, Buying 5, Your stuff → Characters 12, Your stuff → Traits 12, History 5.
- **Open by default:** Needs you, Selling, Buying, Your stuff. **History starts collapsed.**
- **`value === 'None'`** Closet rows stay visible, dimmed, actionless, sorted last. (The `(#516 D5)` tag in the code is an internal review-finding ID, not GitHub issue 516.)
- **Every price label carries its own currency** — characters XRP, traits BRIX. Never sort or compare across currencies.
- **Touch targets:** every action control renders at `min-height: 40px`.
- Run the gate with the repo venv: `.venv/bin/python -m pytest …`. Never `pip install` into `.venv` — it is the deploy box's live prod environment.

---

## File Structure

| File | Responsibility |
|---|---|
| `webapp/client/mine_pure.js` (create) | `buildMine()` — the whole partition, merge, sort, cap and label policy. DOM-free. |
| `tests/test_mine_pure_js.py` (create) | Node-executed unit tests of `buildMine()`, same harness shape as `tests/test_closet_market_pure_js.py`. |
| `webapp/client/index.html` (modify) | Nine `.trait-strip-section` blocks under `#market-mine` → five `<details class="mine-group">` + the `#mine-all` drill-in container. Cache-buster bumps. |
| `webapp/client/style.v43.css` (rename from `style.v42.css`) | New `.mine-group` / `.mine-row` / `.mine-card-action` / `#mine-all` rules. |
| `webapp/client/app.js` (modify) | `renderMine()`, `renderMineCard()`, `renderMineRow()`, `showMineAll()`, the action handler table and the open-state `Set`. Deletes `renderMineGroups` / `renderBidGroups` and `loadClosetMine`'s three chip calls. |
| `tests/test_app_js_deeplink.py` (modify) | The `app.js?v=110` exact pin. |

---

## Reference: the three payloads `buildMine` consumes

Copied from the live server so fixtures are accurate. Fields not listed are ignored by `buildMine`.

`GET /api/market/mine` → `mine`:
```json
{
  "listings": [
    {"kind":"character","nft_id":"00080000AB…","nft_number":1035,"image":"https://cdn/…png",
     "amount_xrp":"12","amount_brix":null,"offer_index":"E3F…","seller":"rMe…","source":"index"},
    {"kind":"trait","nft_id":"00090000CD…","slot":"Hat","value":"Wizard Hat","image":"/api/layer?…",
     "amount_xrp":null,"amount_brix":"25","offer_index":"A11…","seller":"rMe…"}
  ],
  "unlisted_characters": [
    {"nft_id":"00080000EF…","nft_number":2048,"image":"https://cdn/…png","video":null,"attributes":[]}
  ],
  "unlisted_trait_tokens": [
    {"nft_id":"00090000FF…","slot":"Hat","value":"Wizard Hat","image_url":"/api/layer?…"}
  ],
  "closet_assets": [
    {"slot":"Hat","value":"Wizard Hat","count":3,"listed":2,"image_url":"/api/layer?…"},
    {"slot":"Hat","value":"None","count":1,"listed":0,"image_url":null}
  ]
}
```
Note: a **character** listing's art field is `image` (absolute CDN URL); a **trait** listing's art field is also `image` but holds a `/api/layer?…` URL; `unlisted_trait_tokens` and `closet_assets` use `image_url`. `buildMine` normalizes this: it emits `image` for CDN character art and `imageUrl` for `/api/layer` trait art, and `app.js` picks the resolver (`imgUrl` vs `traitLayerSrc`).

`GET /api/market/bids/mine` → `bids`:
```json
{
  "my_bids": [{"offer_index":"B1…","nft_id":"00080000AB…","nft_number":1035,"image":"https://cdn/…png",
               "bidder":"rMe…","amount_xrp":"9","amount_drops":"9000000","expiration":1790000000,
               "fee_cover":null}],
  "bids_on_my_nfts": [{"offer_index":"B2…","nft_id":"00080000EF…","nft_number":2048,
                       "image":"https://cdn/…png","bidder":"rOther…","amount_xrp":"14",
                       "amount_drops":"14000000","expiration":null}]
}
```

`GET /api/closet/orders/mine` → `closet` (absent/`{}` when `closetMarketEnabled` is false):
```json
{
  "orders": [{"id":"o1","side":"ask","slot":"Hat","value":"Wizard Hat","price_brix":"25",
              "state":"open","created_ts":"2026-09-19T10:00:00Z","cancel_after":null,
              "error":null,"image_url":"/api/layer?…"}],
  "bids_on_my_traits": [{"id":"o9","slot":"Eyes","value":"Laser","price_brix":"40",
                         "cancel_after":null,"source":"closet","nft_id":null,
                         "image_url":"/api/layer?…"}],
  "fills": [{"id":"f1","seller":"rOther…","buyer":"rMe…","slot":"Hat","value":"Wizard Hat",
             "price_brix":"25","fee_brix":"1.75","overshoot_brix":"0","state":"mirrored",
             "error":null,"created_ts":"2026-09-18T10:00:00Z","image_url":"/api/layer?…"}]
}
```
`orders[].state` is one of `pending_escrow` | `open` | `matched` | `cancelling` (the store returns no others). `fills[].state` is one of `funds_pending` | `funded` | `asset_moved` | `paid` | `mirrored` | `refund_pending` | `refunded` | `indeterminate` | `failed`.

---

### The `Item` shape (produced by Task 1, consumed by every later task)

```js
{
  unit: 'card' | 'row',
  key: string,                  // stable, unique within its group; used as the DOM key
  image: string | null,         // absolute CDN character art -> app.js calls imgUrl()
  imageUrl: string | null,      // /api/layer trait art    -> app.js calls traitLayerSrc()
  title: string,
  subtitle: string | null,
  price: { amount: string, currency: 'XRP' | 'BRIX' } | null,
  badges: string[],             // e.g. ['in your wallet'], ['×3 · 2 listed']
  state: { label: string } | null,  // e.g. {label: 'filling'}
  dim: boolean,                 // true -> rendered greyed and actionless
  action: { label: string, kind: string, payload: object } | null,
}
```

`action.kind` is exactly one of:
`'cancelListing' | 'cancelBid' | 'acceptBid' | 'list' | 'sell' | 'fillClosetBid' | 'cancelClosetOrder' | 'viewClosetFill' | 'resumeClosetOrder' | 'resumeFill'`.

---

## Task 1: `mine_pure.js` — item shape and the "Your stuff" groups

**Files:**
- Create: `webapp/client/mine_pure.js`
- Create: `tests/test_mine_pure_js.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `buildMine({ mine, bids, closet, wallet, closetMarketEnabled })` returning `{ needsYou, selling, buying, stuff: { characters, traits }, history, ownsNothing, nothingActive }`. Later tasks fill the groups this task leaves empty. Also exports the cap constant map `CAPS` and the helper `capGroup(items, cap)`.

- [ ] **Step 1: Write the failing test file**

Create `tests/test_mine_pure_js.py`:

```python
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/mine_pure.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed on this host")


def run_js(expr: str):
    script = (
        f"import * as M from {json.dumps(MODULE_REL)};\n"
        f"const result = ({expr});\n"
        "console.log(JSON.stringify(result === undefined ? null : result));\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module"], input=script, capture_output=True, text=True, cwd=ROOT
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def build(mine=None, bids=None, closet=None, wallet="rMe", enabled=True):
    args = json.dumps(
        {
            "mine": mine or {},
            "bids": bids or {},
            "closet": closet or {},
            "wallet": wallet,
            "closetMarketEnabled": enabled,
        }
    )
    return run_js(f"M.buildMine({args})")


CHAR = {"nft_id": "0008AB", "nft_number": 1035, "image": "https://cdn/1035.png"}
TRAIT_TOKEN = {"nft_id": "0009FF", "slot": "Hat", "value": "Wizard Hat", "image_url": "/api/layer?a"}
CLOSET_ASSET = {"slot": "Hat", "value": "Wizard Hat", "count": 3, "listed": 2, "image_url": "/api/layer?a"}


def test_empty_payload_owns_nothing():
    out = build()
    assert out["ownsNothing"] is True
    assert out["nothingActive"] is True
    assert out["stuff"]["characters"] == []
    assert out["stuff"]["traits"] == []


def test_unlisted_character_becomes_a_card_with_a_list_action():
    out = build(mine={"unlisted_characters": [CHAR]})
    (item,) = out["stuff"]["characters"]
    assert item["unit"] == "card"
    assert item["title"] == "#1035"
    assert item["image"] == "https://cdn/1035.png"
    assert item["imageUrl"] is None
    assert item["price"] is None
    assert item["action"] == {
        "label": "List",
        "kind": "list",
        "payload": {"nftId": "0008AB", "label": "#1035", "wizard": False},
    }
    assert out["ownsNothing"] is False


def test_character_without_a_number_falls_back_to_its_nft_id():
    out = build(mine={"unlisted_characters": [{"nft_id": "0008CD", "nft_number": None, "image": None}]})
    (item,) = out["stuff"]["characters"]
    assert item["title"] == "0008CD"
    assert item["image"] is None


def test_closet_and_wallet_copies_of_one_trait_are_separate_tiles_by_custody():
    out = build(mine={"closet_assets": [CLOSET_ASSET], "unlisted_trait_tokens": [TRAIT_TOKEN]})
    traits = out["stuff"]["traits"]
    assert len(traits) == 2
    closet = next(t for t in traits if "in your wallet" not in t["badges"])
    wallet = next(t for t in traits if "in your wallet" in t["badges"])
    assert closet["title"] == "Hat: Wizard Hat"
    assert closet["badges"] == ["×3 · 2 listed"]
    assert closet["action"]["kind"] == "sell"
    assert wallet["badges"] == ["in your wallet", "×1"]
    assert wallet["action"]["kind"] == "sell"
    assert closet["key"] != wallet["key"]


def test_several_wallet_tokens_of_one_key_roll_up_into_one_tile():
    tokens = [
        {"nft_id": "a", "slot": "Hat", "value": "Wizard Hat", "image_url": "/api/layer?a"},
        {"nft_id": "b", "slot": "Hat", "value": "Wizard Hat", "image_url": "/api/layer?a"},
    ]
    out = build(mine={"unlisted_trait_tokens": tokens})
    (item,) = out["stuff"]["traits"]
    assert item["badges"] == ["in your wallet", "×2"]
    assert item["action"]["payload"]["nftId"] == "a"


def test_a_fully_unencumbered_closet_tile_shows_only_its_count():
    out = build(mine={"closet_assets": [{**CLOSET_ASSET, "listed": 0}]})
    (item,) = out["stuff"]["traits"]
    assert item["badges"] == ["×3"]


def test_none_value_is_dimmed_actionless_and_last():
    assets = [
        {"slot": "Hat", "value": "None", "count": 1, "listed": 0, "image_url": None},
        CLOSET_ASSET,
    ]
    out = build(mine={"closet_assets": assets})
    titles = [t["title"] for t in out["stuff"]["traits"]]
    assert titles == ["Hat: Wizard Hat", "Hat: None"]
    none_tile = out["stuff"]["traits"][-1]
    assert none_tile["dim"] is True
    assert none_tile["action"] is None


def test_sell_payload_uses_the_closet_ask_path_only_when_the_market_is_on():
    on = build(mine={"closet_assets": [CLOSET_ASSET]}, enabled=True)
    off = build(mine={"closet_assets": [CLOSET_ASSET]}, enabled=False)
    assert on["stuff"]["traits"][0]["action"]["payload"] == {
        "slot": "Hat", "value": "Wizard Hat", "label": "Hat: Wizard Hat",
        "wizard": False, "closetAsk": True,
    }
    assert off["stuff"]["traits"][0]["action"]["payload"]["wizard"] is True
    assert off["stuff"]["traits"][0]["action"]["payload"]["closetAsk"] is False


def test_cap_group_returns_the_head_and_the_remainder_count():
    assert run_js("M.capGroup([1,2,3,4,5], 2)") == {"items": [1, 2], "total": 5, "hidden": 3}
    assert run_js("M.capGroup([1,2], 5)") == {"items": [1, 2], "total": 2, "hidden": 0}
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_mine_pure_js.py -x -q`
Expected: FAIL — `Cannot find module …/webapp/client/mine_pure.js`.

- [ ] **Step 3: Write `mine_pure.js`**

Create `webapp/client/mine_pure.js`:

```js
// webapp/client/mine_pure.js
// The Marketplace "Mine" tab's whole partition/merge/sort/cap policy, kept
// DOM-free so tests/test_mine_pure_js.py can execute it under Node — the same
// posture as market_pure.js and closet_market_pure.js.
//
// buildMine() turns the three Mine payloads into five intent groups named for
// what the user is trying to do, not for the table a row came from. Items
// carry RAW image fields: `image` for absolute CDN character art and
// `imageUrl` for a server /api/layer trait URL, because resolving those is
// surface-dependent (imgUrl vs traitLayerSrc) and belongs in app.js.

export const CAPS = {
  needsYou: 20,
  selling: 12,
  buying: 5,
  characters: 12,
  traits: 12,
  history: 5,
};

export function capGroup(items, cap) {
  return { items: items.slice(0, cap), total: items.length, hidden: Math.max(0, items.length - cap) };
}

function characterTitle(row) {
  return row.nft_number != null ? `#${row.nft_number}` : row.nft_id;
}

function traitTitle(slot, value) {
  return `${slot}: ${value}`;
}

function item(fields) {
  return {
    unit: 'card',
    key: '',
    image: null,
    imageUrl: null,
    title: '',
    subtitle: null,
    price: null,
    badges: [],
    state: null,
    dim: false,
    action: null,
    ...fields,
  };
}

function unlistedCharacters(mine) {
  return (mine.unlisted_characters || []).map((c) => {
    const title = characterTitle(c);
    return item({
      unit: 'card',
      key: `char:${c.nft_id}`,
      image: c.image || null,
      title,
      action: { label: 'List', kind: 'list', payload: { nftId: c.nft_id, label: title, wizard: false } },
    });
  });
}

// A trait you own is one fact; Closet credit vs wallet NFToken is plumbing.
// Both merge into one grid, keyed (slot, value, custody) so at most two tiles
// share a trait key and a tile's badge describes the whole tile.
function traits(mine, closetMarketEnabled) {
  const out = [];
  for (const a of mine.closet_assets || []) {
    const isNone = a.value === 'None';
    const count = `×${a.count}`;
    out.push(item({
      unit: 'card',
      key: `closet:${a.slot}:${a.value}`,
      imageUrl: a.image_url || null,
      title: traitTitle(a.slot, a.value),
      badges: [a.listed ? `${count} · ${a.listed} listed` : count],
      // (#516 D5) "None" is an empty slot, not a tradeable trait: visible so
      // the holding is honest, dimmed and actionless because Sell can do
      // nothing with it.
      dim: isNone,
      action: isNone ? null : {
        label: 'Sell',
        kind: 'sell',
        payload: {
          slot: a.slot,
          value: a.value,
          label: traitTitle(a.slot, a.value),
          wizard: !closetMarketEnabled,
          closetAsk: !!closetMarketEnabled,
        },
      },
    }));
  }
  const byKey = new Map();
  for (const t of mine.unlisted_trait_tokens || []) {
    const key = `${t.slot}:${t.value}`;
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(t);
  }
  for (const [key, tokens] of byKey) {
    const first = tokens[0];
    out.push(item({
      unit: 'card',
      key: `token:${key}`,
      imageUrl: first.image_url || null,
      title: traitTitle(first.slot, first.value),
      badges: ['in your wallet', `×${tokens.length}`],
      action: {
        label: 'Sell',
        kind: 'sell',
        payload: {
          nftId: first.nft_id,
          slot: first.slot,
          value: first.value,
          label: traitTitle(first.slot, first.value),
          wizard: false,
        },
      },
    }));
  }
  // An empty slot is a holding, not a trait — it sorts last, always.
  return out.sort((a, b) => (a.dim === b.dim ? 0 : a.dim ? 1 : -1));
}

export function buildMine({ mine = {}, bids = {}, closet = {}, wallet = null, closetMarketEnabled = false } = {}) {
  const stuff = {
    characters: unlistedCharacters(mine),
    traits: traits(mine, closetMarketEnabled),
  };
  const needsYou = [];
  const selling = [];
  const buying = [];
  const history = [];
  const active = needsYou.length + selling.length + buying.length;
  const owned = active + history.length + stuff.characters.length + stuff.traits.length;
  return {
    needsYou,
    selling,
    buying,
    stuff,
    history,
    ownsNothing: owned === 0,
    nothingActive: active === 0,
  };
}
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv/bin/python -m pytest tests/test_mine_pure_js.py -x -q`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add webapp/client/mine_pure.js tests/test_mine_pure_js.py
git commit -m "feat(mine): add mine_pure with the merged Your-stuff groups"
```

---

## Task 2: Selling and Buying groups

**Files:**
- Modify: `webapp/client/mine_pure.js`
- Modify: `tests/test_mine_pure_js.py`

**Interfaces:**
- Consumes: `item()`, `characterTitle()`, `traitTitle()`, `buildMine()` from Task 1.
- Produces: `buildMine().selling` and `.buying` populated. Selling holds character listings (XRP), trait listings (BRIX) and Closet asks (BRIX) as **cards** in one grid. Buying holds native bids and Closet bids as **rows**.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mine_pure_js.py`:

```python
CHAR_LISTING = {
    "kind": "character", "nft_id": "0008AB", "nft_number": 1035,
    "image": "https://cdn/1035.png", "amount_xrp": "12", "amount_brix": None,
    "offer_index": "E3F", "seller": "rMe",
}
TRAIT_LISTING = {
    "kind": "trait", "nft_id": "0009CD", "slot": "Hat", "value": "Wizard Hat",
    "image": "/api/layer?a", "amount_xrp": None, "amount_brix": "25",
    "offer_index": "A11", "seller": "rMe",
}
ASK = {
    "id": "o1", "side": "ask", "slot": "Hat", "value": "Wizard Hat",
    "price_brix": "25", "state": "open", "created_ts": "2026-09-19T10:00:00Z",
    "image_url": "/api/layer?a",
}
CLOSET_BID = {**ASK, "id": "o2", "side": "bid", "state": "open", "price_brix": "9"}
MY_BID = {
    "offer_index": "B1", "nft_id": "0008AB", "nft_number": 1035,
    "image": "https://cdn/1035.png", "bidder": "rMe", "amount_xrp": "9",
}


def test_selling_holds_both_currencies_as_cards_in_one_group():
    out = build(mine={"listings": [CHAR_LISTING, TRAIT_LISTING]})
    assert [i["unit"] for i in out["selling"]] == ["card", "card"]
    char, trait = out["selling"]
    assert char["title"] == "#1035"
    assert char["price"] == {"amount": "12", "currency": "XRP"}
    assert char["image"] == "https://cdn/1035.png" and char["imageUrl"] is None
    assert trait["title"] == "Hat: Wizard Hat"
    assert trait["price"] == {"amount": "25", "currency": "BRIX"}
    assert trait["imageUrl"] == "/api/layer?a" and trait["image"] is None
    assert char["action"]["kind"] == "cancelListing"
    assert char["action"]["payload"]["offer_index"] == "E3F"


def test_a_closet_ask_sells_beside_the_nft_listings():
    out = build(closet={"orders": [ASK]})
    (item,) = out["selling"]
    assert item["unit"] == "card"
    assert item["title"] == "Hat: Wizard Hat"
    assert item["price"] == {"amount": "25", "currency": "BRIX"}
    assert item["action"] == {
        "label": "Cancel", "kind": "cancelClosetOrder",
        "payload": {"id": "o1", "side": "ask"},
    }


def test_closet_orders_split_by_side():
    out = build(closet={"orders": [ASK, CLOSET_BID]})
    assert [i["title"] for i in out["selling"]] == ["Hat: Wizard Hat"]
    assert [i["title"] for i in out["buying"]] == ["Hat: Wizard Hat"]
    assert out["buying"][0]["unit"] == "row"
    assert out["buying"][0]["action"]["payload"] == {"id": "o2", "side": "bid"}


def test_matched_and_cancelling_orders_stay_put_with_a_state_chip_and_no_action():
    out = build(closet={"orders": [{**ASK, "state": "matched"}, {**CLOSET_BID, "state": "cancelling"}]})
    assert out["selling"][0]["state"] == {"label": "filling"}
    assert out["selling"][0]["action"] is None
    assert out["buying"][0]["state"] == {"label": "cancelling"}
    assert out["buying"][0]["action"] is None
    assert out["needsYou"] == []


def test_my_native_bids_are_buying_rows():
    out = build(bids={"my_bids": [MY_BID]})
    (item,) = out["buying"]
    assert item["unit"] == "row"
    assert item["title"] == "#1035"
    assert item["price"] == {"amount": "9", "currency": "XRP"}
    assert item["action"]["kind"] == "cancelBid"
    assert item["action"]["payload"]["offer_index"] == "B1"
    assert out["nothingActive"] is False
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_mine_pure_js.py -x -q`
Expected: FAIL — `out["selling"]` is `[]`.

- [ ] **Step 3: Implement**

In `webapp/client/mine_pure.js`, add above `buildMine`:

```js
const ORDER_STATE_TEXT = {
  pending_escrow: 'awaiting signature', open: 'open', matched: 'filling', cancelling: 'cancelling',
};

// matched ("filling") and cancelling admit no user action — they stay in
// Selling/Buying wearing a state chip rather than climbing into Needs you.
function orderItem(order, unit) {
  const actionable = order.state === 'open';
  return item({
    unit,
    key: `order:${order.id}`,
    imageUrl: order.image_url || null,
    title: traitTitle(order.slot, order.value),
    price: { amount: order.price_brix, currency: 'BRIX' },
    state: order.state === 'open' ? null : { label: ORDER_STATE_TEXT[order.state] ?? order.state },
    action: actionable
      ? { label: 'Cancel', kind: 'cancelClosetOrder', payload: { id: order.id, side: order.side } }
      : null,
  });
}

function listingItem(row) {
  const isTrait = row.kind === 'trait';
  return item({
    unit: 'card',
    key: `listing:${row.offer_index}`,
    image: isTrait ? null : row.image || null,
    imageUrl: isTrait ? row.image || null : null,
    title: isTrait ? traitTitle(row.slot, row.value) : characterTitle(row),
    price: isTrait
      ? { amount: row.amount_brix, currency: 'BRIX' }
      : { amount: row.amount_xrp, currency: 'XRP' },
    action: { label: 'Cancel', kind: 'cancelListing', payload: row },
  });
}

function bidItem(bid, { kind, label }) {
  return item({
    unit: 'row',
    key: `bid:${bid.offer_index}`,
    image: bid.image || null,
    title: characterTitle(bid),
    price: { amount: bid.amount_xrp, currency: 'XRP' },
    action: { label, kind, payload: bid },
  });
}
```

Then replace the four empty arrays in `buildMine` with:

```js
  const liveOrders = closet.orders || [];
  const selling = [
    ...(mine.listings || []).map(listingItem),
    ...liveOrders.filter((o) => o.side === 'ask' && o.state !== 'pending_escrow').map((o) => orderItem(o, 'card')),
  ];
  const buying = [
    ...(bids.my_bids || []).map((b) => bidItem(b, { kind: 'cancelBid', label: 'Cancel' })),
    ...liveOrders.filter((o) => o.side === 'bid' && o.state !== 'pending_escrow').map((o) => orderItem(o, 'row')),
  ];
  const needsYou = [];
  const history = [];
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv/bin/python -m pytest tests/test_mine_pure_js.py -x -q`
Expected: PASS, 14 tests.

- [ ] **Step 5: Commit**

```bash
git add webapp/client/mine_pure.js tests/test_mine_pure_js.py
git commit -m "feat(mine): partition Selling and Buying in mine_pure"
```

---

## Task 3: Needs you, History, and the totality guarantee

**Files:**
- Modify: `webapp/client/mine_pure.js`
- Modify: `tests/test_mine_pure_js.py`

**Interfaces:**
- Consumes: everything from Tasks 1–2.
- Produces: `buildMine().needsYou` and `.history` populated; `ownsNothing` / `nothingActive` correct across all five groups. This completes `buildMine` — no later task changes it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mine_pure_js.py`:

```python
INCOMING_NFT_BID = {
    "offer_index": "B2", "nft_id": "0008EF", "nft_number": 2048,
    "image": "https://cdn/2048.png", "bidder": "rOther", "amount_xrp": "14",
}
INCOMING_TRAIT_BID = {
    "id": "o9", "slot": "Eyes", "value": "Laser", "price_brix": "40",
    "source": "closet", "nft_id": None, "image_url": "/api/layer?e",
}
FILL_DONE = {
    "id": "f1", "seller": "rOther", "buyer": "rMe", "slot": "Hat", "value": "Wizard Hat",
    "price_brix": "25", "state": "mirrored", "created_ts": "2026-09-18T10:00:00Z",
    "image_url": "/api/layer?a",
}


def test_incoming_nft_bids_lead_needs_you_highest_first():
    high = {**INCOMING_NFT_BID, "offer_index": "B3", "amount_xrp": "30"}
    out = build(bids={"bids_on_my_nfts": [INCOMING_NFT_BID, high]})
    assert [i["price"]["amount"] for i in out["needsYou"]] == ["30", "14"]
    assert out["needsYou"][0]["unit"] == "row"
    assert out["needsYou"][0]["action"]["kind"] == "acceptBid"


def test_character_bids_group_before_trait_bids_never_sorted_across_currencies():
    out = build(
        bids={"bids_on_my_nfts": [INCOMING_NFT_BID]},
        closet={"bids_on_my_traits": [INCOMING_TRAIT_BID]},
    )
    assert [i["price"]["currency"] for i in out["needsYou"]] == ["XRP", "BRIX"]


def test_a_wallet_held_trait_bid_discloses_the_deposit_at_action_time():
    token_bid = {**INCOMING_TRAIT_BID, "source": "token", "nft_id": "0009FF"}
    out = build(closet={"bids_on_my_traits": [token_bid]})
    (item,) = out["needsYou"]
    assert item["action"]["label"] == "Deposit & fill"
    assert item["badges"] == ["in your wallet"]
    plain = build(closet={"bids_on_my_traits": [INCOMING_TRAIT_BID]})["needsYou"][0]
    assert plain["action"]["label"] == "Fill"
    assert plain["badges"] == []


def test_a_half_signed_bid_is_something_only_the_user_can_finish():
    # Only a BID is ever pending_escrow — an ask is off-ledger and opens
    # instantly, so it has no signature to finish.
    out = build(closet={"orders": [{**CLOSET_BID, "state": "pending_escrow"}]})
    (item,) = out["needsYou"]
    assert item["action"] == {
        "label": "Finish signing", "kind": "resumeClosetOrder",
        "payload": {"id": "o2", "side": "bid"},
    }
    assert out["buying"] == []


def test_a_fill_waiting_on_my_money_or_stuck_needs_me_everything_else_is_history():
    fills = [
        {**FILL_DONE, "id": "f2", "state": "funds_pending", "buyer": "rMe"},
        {**FILL_DONE, "id": "f3", "state": "funds_pending", "buyer": "rOther", "seller": "rMe"},
        {**FILL_DONE, "id": "f4", "state": "failed"},
        FILL_DONE,
    ]
    out = build(closet={"fills": fills})
    assert [i["key"] for i in out["needsYou"]] == ["fill:f2", "fill:f4"]
    assert [i["action"]["kind"] for i in out["needsYou"]] == ["resumeFill", "viewClosetFill"]
    assert sorted(i["key"] for i in out["history"]) == ["fill:f1", "fill:f3"]
    assert all(i["unit"] == "row" for i in out["history"])


def test_history_rows_say_which_side_i_was_on():
    out = build(closet={"fills": [FILL_DONE]}, wallet="rMe")
    (item,) = out["history"]
    assert item["subtitle"] == "Bought · Complete"
    sold = build(closet={"fills": [{**FILL_DONE, "buyer": "rOther", "seller": "rMe"}]}, wallet="rMe")
    assert sold["history"][0]["subtitle"] == "Sold · Complete"


def test_the_partition_is_total_and_disjoint():
    out = build(
        mine={
            "listings": [CHAR_LISTING, TRAIT_LISTING],
            "unlisted_characters": [CHAR],
            "unlisted_trait_tokens": [TRAIT_TOKEN],
            "closet_assets": [CLOSET_ASSET],
        },
        bids={"my_bids": [MY_BID], "bids_on_my_nfts": [INCOMING_NFT_BID]},
        closet={
            "orders": [ASK, CLOSET_BID, {**CLOSET_BID, "id": "o3", "state": "pending_escrow"}],
            "bids_on_my_traits": [INCOMING_TRAIT_BID],
            "fills": [FILL_DONE, {**FILL_DONE, "id": "f4", "state": "failed"}],
        },
    )
    groups = out["needsYou"] + out["selling"] + out["buying"] + out["history"] \
        + out["stuff"]["characters"] + out["stuff"]["traits"]
    keys = [i["key"] for i in groups]
    assert len(keys) == len(set(keys)), "a row landed in two groups"
    # every input row produced exactly one item: 2 listings + 1 char + 1 token
    # + 1 closet asset + 1 my_bid + 1 incoming bid + 3 orders + 1 trait bid
    # + 2 fills = 13
    assert len(keys) == 13


def test_nothing_active_is_true_while_holdings_exist():
    out = build(mine={"unlisted_characters": [CHAR]})
    assert out["ownsNothing"] is False
    assert out["nothingActive"] is True


def test_the_closet_market_being_off_simply_yields_empty_groups():
    out = build(mine={"unlisted_characters": [CHAR]}, closet={}, enabled=False)
    assert out["selling"] == [] and out["buying"] == [] and out["needsYou"] == []
    assert out["history"] == []
    assert len(out["stuff"]["characters"]) == 1
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_mine_pure_js.py -x -q`
Expected: FAIL — `out["needsYou"]` is `[]`.

- [ ] **Step 3: Implement**

In `webapp/client/mine_pure.js`, add:

```js
const FILL_STATE_TEXT = {
  funds_pending: 'Waiting for funds', funded: 'Moving the trait', asset_moved: 'Paying the seller',
  paid: 'Updating Closets', mirrored: 'Complete', refund_pending: 'Refunding', refunded: 'Refunded',
  indeterminate: 'Confirming on-ledger', failed: 'Failed',
};

function fillItem(fill, wallet) {
  const role = fill.buyer === wallet ? 'Bought' : 'Sold';
  return item({
    unit: 'row',
    key: `fill:${fill.id}`,
    imageUrl: fill.image_url || null,
    title: traitTitle(fill.slot, fill.value),
    subtitle: `${role} · ${FILL_STATE_TEXT[fill.state] ?? fill.state}`,
    price: { amount: fill.price_brix, currency: 'BRIX' },
    action: { label: 'View', kind: 'viewClosetFill', payload: fill },
  });
}

// A fill the buyer still owes money on is the user's move; a failed one is
// theirs to look at. Everything else is the market working — history.
function fillNeedsUser(fill, wallet) {
  if (fill.state === 'failed') return true;
  return fill.state === 'funds_pending' && fill.buyer === wallet;
}

function holdingBidItem(bid) {
  const inWallet = bid.source === 'token';
  return item({
    unit: 'row',
    key: `holdingbid:${bid.id}`,
    imageUrl: bid.image_url || null,
    title: traitTitle(bid.slot, bid.value),
    price: { amount: bid.price_brix, currency: 'BRIX' },
    badges: inWallet ? ['in your wallet'] : [],
    action: {
      label: inWallet ? 'Deposit & fill' : 'Fill',
      kind: 'fillClosetBid',
      payload: bid,
    },
  });
}

function byPriceDesc(a, b) {
  return Number(b.price.amount) - Number(a.price.amount);
}
```

Then, inside `buildMine`, replace the `needsYou` / `history` placeholders:

```js
  const fills = closet.fills || [];
  // Incoming offers first — money waiting on a decision — grouped by kind
  // before sorting, because ranking XRP against BRIX would be meaningless.
  const needsYou = [
    ...(bids.bids_on_my_nfts || [])
      .map((b) => bidItem(b, { kind: 'acceptBid', label: 'Accept' }))
      .sort(byPriceDesc),
    ...(closet.bids_on_my_traits || []).map(holdingBidItem).sort(byPriceDesc),
    // …then the user's own stuck actions, oldest first.
    ...liveOrders
      .filter((o) => o.state === 'pending_escrow')
      .map((o) => ({
        ...orderItem(o, 'row'),
        state: null,
        action: { label: 'Finish signing', kind: 'resumeClosetOrder', payload: { id: o.id, side: o.side } },
      })),
    ...fills
      .filter((f) => fillNeedsUser(f, wallet))
      .map((f) => {
        const row = fillItem(f, wallet);
        return f.state === 'failed'
          ? row
          : { ...row, action: { label: 'Finish paying', kind: 'resumeFill', payload: f } };
      }),
  ];
  const history = fills.filter((f) => !fillNeedsUser(f, wallet)).map((f) => fillItem(f, wallet));
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv/bin/python -m pytest tests/test_mine_pure_js.py -q`
Expected: PASS, 23 tests.

- [ ] **Step 5: Commit**

```bash
git add webapp/client/mine_pure.js tests/test_mine_pure_js.py
git commit -m "feat(mine): complete the buildMine partition with Needs you and History"
```

---

## Task 4: CSS and the markup shell

**Files:**
- Rename: `webapp/client/style.v42.css` → `webapp/client/style.v43.css`
- Modify: `webapp/client/index.html`
- Modify: `tests/test_app_js_deeplink.py:86`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure presentation).
- Produces: the DOM ids Task 5 renders into — `mine-group-needsYou`, `mine-group-selling`, `mine-group-buying`, `mine-group-characters`, `mine-group-traits`, `mine-group-history` (each a `<details>`), each holding `<summary class="mine-group-title">` with `.mine-group-name` / `.mine-group-count` spans and a body `<div id="mine-body-<key>">`, plus `mine-nothing` (the muted line), `mine-empty` (the owns-nothing block with `#mine-browse-btn`) and the `#mine-all` drill-in (`#mine-all-title`, `#mine-all-search`, `#mine-all-body`, `#mine-all-back`).

- [ ] **Step 1: Rename the stylesheet and repoint every reference**

```bash
git mv webapp/client/style.v42.css webapp/client/style.v43.css
sed -i 's/style\.v42\.css/style.v43.css/' webapp/client/index.html
grep -rn "style\.v4" webapp/client/index.html tests/ webapp/*.py
```
Expected: only `style.v43.css` remains.

- [ ] **Step 2: Append the Mine rules to `webapp/client/style.v43.css`**

```css
/* --- Marketplace > Mine: five intent groups (#583) --- */
.mine-group {
  margin-top: 12px; text-align: left;
  border: 1px solid var(--line); border-radius: 10px; background: var(--surface);
}
/* The whole summary row is the hit target — same pattern as .odds-slot-title. */
.mine-group-title {
  display: flex; align-items: center; gap: 8px;
  margin: 0; padding: 10px 12px; font-size: .95rem; font-weight: 700;
  color: var(--text); cursor: pointer; list-style: none; user-select: none;
  border-radius: 10px; min-height: 40px; box-sizing: border-box;
}
.mine-group-title::-webkit-details-marker { display: none; }
.mine-group-title::after {
  content: '\25be'; margin-left: auto; color: var(--muted); font-size: .9rem;
  transition: transform .15s ease;
}
.mine-group[open] > .mine-group-title::after { transform: rotate(180deg); }
.mine-group[open] > .mine-group-title { border-bottom: 1px solid var(--line); border-radius: 10px 10px 0 0; }
.mine-group-title:hover { background: var(--surface-2); }
.mine-group-title:focus-visible { outline: 2px solid var(--accent, var(--text)); outline-offset: 2px; }
.mine-group-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mine-group-count { flex: 0 0 auto; color: var(--muted); font-size: .75rem; font-weight: 600; }
.mine-group-body { padding: 10px 12px; }

/* Rows: offers and trade events, where art identifies nothing. */
.mine-rows { display: flex; flex-direction: column; gap: 8px; }
.mine-row {
  display: grid; grid-template-columns: 44px 1fr auto auto; gap: 10px;
  align-items: center; padding: 6px 8px; border-radius: 8px; background: var(--surface-2);
  grid-template-areas: 'art label price action';
}
.mine-row img, .mine-row video {
  grid-area: art; width: 44px; height: 44px; object-fit: contain;
  border-radius: 6px; background: var(--surface);
}
.mine-row-label { grid-area: label; min-width: 0; color: var(--text); font-size: .85rem; }
.mine-row-sub { display: block; color: var(--muted); font-size: .68rem; margin-top: 2px; }
.mine-row-price { grid-area: price; color: var(--yellow); font-size: .78rem; font-weight: 700; white-space: nowrap; }
.mine-row .mine-action { grid-area: action; }
.mine-row.dim { opacity: .45; }
@media (max-width: 420px) {
  .mine-row {
    grid-template-columns: 44px 1fr;
    grid-template-areas: 'art label' 'price price' 'action action';
    row-gap: 6px;
  }
  .mine-row .mine-action { width: 100%; }
}

/* Cards: things you hold or are selling, where the art is the point. */
.nft-card .mine-card-price { display: block; color: var(--yellow); font-size: .72rem; font-weight: 700; margin-top: 2px; }
.nft-card .mine-card-badge { display: block; color: var(--muted); font-size: .62rem; font-weight: 700; margin-top: 1px; }
.mine-action {
  min-height: 40px; padding: 6px 12px; margin: 0;
  font-size: .78rem; font-weight: 700; border-radius: 8px;
}
.nft-card .mine-action { width: 100%; border-radius: 0; border: 0; border-top: 3px solid var(--ink); }

.mine-show-all { margin: 10px 0 0; display: block; width: 100%; min-height: 40px; }
.mine-note { margin: 10px 0 0; font-size: .85rem; color: var(--muted); }
#mine-all-search { width: 100%; margin: 0 0 10px; }
#mine-all-title { margin: 0 0 8px; }
```

- [ ] **Step 3: Replace the nine sections in `webapp/client/index.html`**

Replace everything between `<div id="market-mine" class="market-section" hidden>` and its closing `</div>` with:

```html
      <div id="market-mine" class="market-section" hidden>
        <div id="mine-groups">
          <details class="mine-group" id="mine-group-needsYou" open>
            <summary class="mine-group-title"><span class="mine-group-name">Needs you</span><span class="mine-group-count"></span></summary>
            <div class="mine-group-body" id="mine-body-needsYou"></div>
          </details>
          <details class="mine-group" id="mine-group-selling" open>
            <summary class="mine-group-title"><span class="mine-group-name">Selling</span><span class="mine-group-count"></span></summary>
            <div class="mine-group-body" id="mine-body-selling"></div>
          </details>
          <details class="mine-group" id="mine-group-buying" open>
            <summary class="mine-group-title"><span class="mine-group-name">Buying</span><span class="mine-group-count"></span></summary>
            <div class="mine-group-body" id="mine-body-buying"></div>
          </details>
          <details class="mine-group" id="mine-group-characters" open>
            <summary class="mine-group-title"><span class="mine-group-name">Your characters</span><span class="mine-group-count"></span></summary>
            <div class="mine-group-body" id="mine-body-characters"></div>
          </details>
          <details class="mine-group" id="mine-group-traits" open>
            <summary class="mine-group-title"><span class="mine-group-name">Your traits</span><span class="mine-group-count"></span></summary>
            <div class="mine-group-body" id="mine-body-traits"></div>
          </details>
          <details class="mine-group" id="mine-group-history">
            <summary class="mine-group-title"><span class="mine-group-name">History</span><span class="mine-group-count"></span></summary>
            <div class="mine-group-body" id="mine-body-history"></div>
          </details>
          <p id="mine-nothing" class="mine-note" hidden>Nothing listed or bid on yet.</p>
          <div id="mine-empty" hidden>
            <p class="mine-note">You don't own anything yet.</p>
            <button id="mine-browse-btn" class="primary">Browse the marketplace</button>
          </div>
        </div>
        <div id="mine-all" hidden>
          <p class="row-links"><button id="mine-all-back" class="back">← Back</button></p>
          <h4 id="mine-all-title"></h4>
          <input id="mine-all-search" type="text" placeholder="Search…" autocomplete="off">
          <div id="mine-all-body"></div>
        </div>
      </div>
```

- [ ] **Step 4: Bump the `app.js` cache buster and its pinned test**

```bash
sed -i 's|app\.js?v=110|app.js?v=111|' webapp/client/index.html
sed -i 's|app\.js?v=110|app.js?v=111|' tests/test_app_js_deeplink.py
grep -rn "app.js?v=" webapp/client/index.html tests/test_app_js_deeplink.py
```
Expected: both read `v=111`.

- [ ] **Step 5: Run the client DOM/stylesheet tests**

Run: `.venv/bin/python -m pytest tests/test_app_js_deeplink.py tests/test_client_stylesheet.py tests/test_brix_card_dom.py tests/test_closet_market_dom.py -q`
Expected: PASS. If `test_closet_market_dom.py` asserts one of the removed `mine-closet-*` ids, update that assertion to the new id it replaces and note it in the commit message — the ids are the contract those tests pin.

- [ ] **Step 6: Commit**

```bash
git add -A webapp/client tests/test_app_js_deeplink.py
git commit -m "feat(mine): five collapsible intent groups and the drill-in shell"
```

---

## Task 5: `renderMine()` — the DOM glue

**Files:**
- Modify: `webapp/client/app.js`

**Interfaces:**
- Consumes: `buildMine()`, `CAPS`, `capGroup()` (Task 1–3); the DOM ids from Task 4.
- Produces: `renderMine(built)`, `renderMineCard(item)`, `renderMineRow(item)`, `mineOpenGroups` (module-level `Set`), `MINE_ACTIONS` (the `kind` → handler table), and `mineGroupItems` (a module-level object holding each group's **full, uncapped** list, which Task 6's drill-in reads).

- [ ] **Step 1: Import the module and add the action table**

Near the other `_pure.js` imports at the top of `webapp/client/app.js`:

```js
// Mine tab (#583): the group partition, merge, sort and caps are pure and
// Node-tested (tests/test_mine_pure_js.py); renderMine() below is the glue.
import * as minePure from './mine_pure.js?v=1';
```

Then, next to the other Mine helpers (just above `renderMineGroups`), add:

```js
// Which <details> the user has open, so the poll-driven re-render after an
// action cannot collapse what they just opened (mirrors oddsOpenSlots).
const mineOpenGroups = new Set(['needsYou', 'selling', 'buying', 'characters', 'traits']);
// Each group's FULL list, for the "Show all" drill-in.
const mineGroupItems = {};

const MINE_ACTIONS = {
  cancelListing,
  cancelBid,
  acceptBid,
  list: openListForm,
  sell: openListForm,
  fillClosetBid,
  cancelClosetOrder,
  viewClosetFill,
  resumeClosetOrder: (order) => viewClosetOrder(order),
  resumeFill: viewClosetFill,
};

const MINE_GROUP_TITLES = {
  needsYou: 'Needs you', selling: 'Selling', buying: 'Buying',
  characters: 'Your characters', traits: 'Your traits', history: 'History',
};

function mineImgSrc(item) {
  if (item.image) return imgUrl(item.image, THUMB_W);
  if (item.imageUrl) return traitLayerSrc(item.imageUrl);
  return null;
}

function mineActionButton(item, cls) {
  const btn = document.createElement('button');
  btn.className = cls;
  btn.textContent = item.action.label;
  // #133: a handler may be async — Promise.resolve covers the sync ones and
  // stops a rejection from vanishing silently.
  btn.onclick = (ev) => {
    ev.stopPropagation();
    Promise.resolve()
      .then(() => MINE_ACTIONS[item.action.kind](item.action.payload))
      .catch((e) => showError(e.message));
  };
  return btn;
}
```

`viewClosetOrder` does not exist yet — add it beside `viewClosetFill`:

```js
// A pending_escrow order was never signed. Only a BID can be in that state —
// an ask is off-ledger and opens instantly — so this always resumes the bid
// signing flow through the existing GET /api/closet/bid/{id} status route.
async function viewClosetOrder(order) {
  const s = await api(`/api/closet/bid/${order.id}`);
  showFlow(closetBidRender(s));
  if (!marketPure.isMarketTerminal(s.state)) pollMarketFlow('closet_bid', s.id, closetBidRender);
}
```

- [ ] **Step 2: Write the card and row renderers**

```js
function renderMineCard(item) {
  const card = document.createElement('div');
  card.className = item.dim ? 'nft-card dim' : 'nft-card';
  const img = document.createElement('img');
  img.src = mineImgSrc(item) || BLANK_IMG;
  decorateTraitArt(img, img.src);
  img.loading = 'lazy';
  img.alt = '';
  const cap = document.createElement('span');
  cap.className = 'cap';
  cap.textContent = item.title;
  if (item.price) {
    const price = document.createElement('span');
    price.className = 'mine-card-price';
    price.textContent = `${item.price.amount} ${item.price.currency}`;
    cap.appendChild(price);
  }
  for (const text of item.badges) {
    const badge = document.createElement('span');
    badge.className = 'mine-card-badge';
    badge.textContent = text;
    cap.appendChild(badge);
  }
  if (item.state) {
    const chip = document.createElement('span');
    chip.className = 'mine-card-badge';
    chip.textContent = item.state.label;
    cap.appendChild(chip);
  }
  const parts = [img, cap];
  if (item.action) parts.push(mineActionButton(item, 'secondary mine-action'));
  card.replaceChildren(...parts);
  return card;
}

function renderMineRow(item) {
  const row = document.createElement('div');
  row.className = item.dim ? 'mine-row dim' : 'mine-row';
  const img = document.createElement('img');
  img.src = mineImgSrc(item) || BLANK_IMG;
  decorateTraitArt(img, img.src);
  img.loading = 'lazy';
  img.alt = '';
  const label = document.createElement('span');
  label.className = 'mine-row-label';
  label.textContent = item.title;
  const subText = item.subtitle || (item.state ? item.state.label : null)
    || (item.badges.length ? item.badges.join(' · ') : null);
  if (subText) {
    const sub = document.createElement('span');
    sub.className = 'mine-row-sub';
    sub.textContent = subText;
    label.appendChild(sub);
  }
  const price = document.createElement('span');
  price.className = 'mine-row-price';
  price.textContent = item.price ? `${item.price.amount} ${item.price.currency}` : '';
  const parts = [img, label, price];
  if (item.action) parts.push(mineActionButton(item, 'secondary mine-action'));
  row.replaceChildren(...parts);
  return row;
}
```

- [ ] **Step 3: Write `renderMine` and delete the three old renderers**

Delete `renderMineGroups` and `renderBidGroups` entirely, and delete the three `renderChipList` calls in `loadClosetMine` (keep the function, which becomes a fetch). Add:

```js
function renderMineGroup(key, items, unit) {
  mineGroupItems[key] = items;
  const details = el(`mine-group-${key}`);
  const body = el(`mine-body-${key}`);
  // A group with nothing in it renders nothing at all — no header, no
  // italic apology. Eight of those was most of the old tab.
  details.hidden = items.length === 0;
  if (!items.length) { body.replaceChildren(); return; }
  details.open = mineOpenGroups.has(key);
  details.querySelector('.mine-group-count').textContent = String(items.length);
  const { items: shown, hidden } = minePure.capGroup(items, minePure.CAPS[key]);
  const container = document.createElement('div');
  container.className = unit === 'card' ? 'nft-grid' : 'mine-rows';
  container.replaceChildren(...shown.map(unit === 'card' ? renderMineCard : renderMineRow));
  const parts = [container];
  if (hidden > 0) {
    const more = document.createElement('button');
    more.className = 'secondary mine-show-all';
    more.textContent = `Show all ${items.length} →`;
    more.onclick = () => showMineAll(key);
    parts.push(more);
  }
  body.replaceChildren(...parts);
}

function renderMine(built) {
  renderMineGroup('needsYou', built.needsYou, 'row');
  renderMineGroup('selling', built.selling, 'card');
  renderMineGroup('buying', built.buying, 'row');
  renderMineGroup('characters', built.stuff.characters, 'card');
  renderMineGroup('traits', built.stuff.traits, 'card');
  renderMineGroup('history', built.history, 'row');
  el('mine-nothing').hidden = built.ownsNothing || !built.nothingActive;
  el('mine-empty').hidden = !built.ownsNothing;
}
```

Wire the open-state `Set` once, next to the other one-time listener setup (search for where `oddsOpenSlots` is wired and follow that pattern):

```js
for (const key of Object.keys(MINE_GROUP_TITLES)) {
  el(`mine-group-${key}`).addEventListener('toggle', (ev) => {
    if (ev.target.open) mineOpenGroups.add(key); else mineOpenGroups.delete(key);
  });
}
el('mine-browse-btn').onclick = () => switchMarketTab('browse');
```

- [ ] **Step 4: Rewrite `loadMarketMine` to fetch all three and render once**

```js
async function loadMarketMine() {
  let mine = {};
  try {
    mine = await api('/api/market/mine');
  } catch (e) {
    showError(e.message);
  }
  // #283: bids load separately — a bids failure must not blank the listings.
  let bids = {};
  try { bids = await api('/api/market/bids/mine'); } catch (e) { bids = {}; }
  let closet = {};
  if (closetMarketEnabled) {
    try { closet = await api('/api/closet/orders/mine'); } catch (e) { showError(e.message); }
  }
  renderMine(minePure.buildMine({
    mine, bids, closet,
    wallet: me && me.wallet,
    closetMarketEnabled,
  }));
}
```

Delete `loadClosetMine` and its call site (the `loadClosetMine().catch(...)` line in the old `loadMarketMine`). Confirm nothing else calls it:

```bash
grep -n "loadClosetMine\|renderMineGroups\|renderBidGroups" webapp/client/app.js
```
Expected: no matches.

- [ ] **Step 5: Verify `renderChipList` still has its other callers**

Run: `grep -n "renderChipList(" webapp/client/app.js`
Expected: the definition plus the `closet-book-bids` call — Mine no longer appears.

- [ ] **Step 6: Run the client tests**

Run: `.venv/bin/python -m pytest tests/ webapp/ -q -k "app_js or dom or client or market_pure or mine_pure or closet_market"`
Expected: PASS. Any failure naming a deleted `mine-*` id is a test pinning the old markup — update it to the new id.

- [ ] **Step 7: Commit**

```bash
git add webapp/client/app.js
git commit -m "feat(mine): render the five groups as cards and rows"
```

---

## Task 6: The "Show all" drill-in with search

**Files:**
- Modify: `webapp/client/app.js`

**Interfaces:**
- Consumes: `mineGroupItems`, `renderMineCard`, `renderMineRow`, `MINE_GROUP_TITLES` (Task 5); `#mine-all*` ids (Task 4).
- Produces: `showMineAll(groupKey)` and `closeMineAll()`.

- [ ] **Step 1: Implement the drill-in**

```js
// The drill-in replaces the group list entirely, so exactly one scroller is
// ever live — nesting .nft-grid's scroll container inside the page scroll is
// the mobile trap this avoids.
const MINE_GROUP_UNITS = {
  needsYou: 'row', selling: 'card', buying: 'row',
  characters: 'card', traits: 'card', history: 'row',
};

function renderMineAllBody(key, query) {
  const unit = MINE_GROUP_UNITS[key];
  const needle = query.trim().toLowerCase();
  const items = (mineGroupItems[key] || []).filter((i) => !needle
    || i.title.toLowerCase().includes(needle)
    || (i.subtitle || '').toLowerCase().includes(needle));
  const container = document.createElement('div');
  container.className = unit === 'card' ? 'nft-grid' : 'mine-rows';
  container.replaceChildren(...items.map(unit === 'card' ? renderMineCard : renderMineRow));
  el('mine-all-body').replaceChildren(container);
}

function showMineAll(key) {
  const items = mineGroupItems[key] || [];
  el('mine-all-title').textContent = `${MINE_GROUP_TITLES[key]} (${items.length})`;
  const search = el('mine-all-search');
  search.value = '';
  search.oninput = () => renderMineAllBody(key, search.value);
  renderMineAllBody(key, '');
  el('mine-groups').hidden = true;
  el('mine-all').hidden = false;
}

function closeMineAll() {
  el('mine-all').hidden = true;
  el('mine-groups').hidden = false;
}
```

Wire the back control beside the other one-time listeners:

```js
el('mine-all-back').onclick = closeMineAll;
```

- [ ] **Step 2: Close the drill-in whenever the tab re-renders**

At the top of `renderMine`, add:

```js
  // A poll-driven re-render must not leave a drill-in showing a stale list.
  closeMineAll();
```

- [ ] **Step 3: Verify by hand-running the pure filter logic**

Run:
```bash
node --input-type=module -e "
import * as M from './webapp/client/mine_pure.js';
const out = M.buildMine({ mine: { unlisted_characters: Array.from({length: 20}, (_, i) => ({nft_id: 'x'+i, nft_number: 1000+i, image: null})) } });
console.log(JSON.stringify(M.capGroup(out.stuff.characters, M.CAPS.characters).hidden));
"
```
Expected: `8` (20 characters, cap 12).

- [ ] **Step 4: Run the full client test slice**

Run: `.venv/bin/python -m pytest tests/ webapp/ -q -k "app_js or dom or client or mine_pure"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/client/app.js
git commit -m "feat(mine): add the searchable Show-all drill-in"
```

---

## Task 7: Visual verification, full gate and PR

**Files:**
- No source changes expected; fix whatever the screenshots expose.

- [ ] **Step 1: Build the headless harness**

Create `<scratchpad>/mine_shot.mjs` (the scratchpad dir, never the repo — `conftest.py`'s store-isolation audit fails any test-adjacent write into the checkout):

```js
import { chromium } from '/home/hamsa/LFG/scripts/share_card/node_modules/playwright/index.mjs';
import { readFileSync } from 'node:fs';

const CLIENT = '/home/hamsa/LFG-wt-mine/webapp/client';
const TYPES = { html: 'text/html', js: 'text/javascript', css: 'text/css', svg: 'image/svg+xml' };

// Three holder shapes from the spec's measurements.
const SHAPES = {
  tiny: { chars: 1, traits: 0, closet: 0 },
  mid: { chars: 24, traits: 6, closet: 18 },
  whale: { chars: 349, traits: 0, closet: 475 },
};

function payloads({ chars, traits, closet }) {
  return {
    '/api/market/mine': {
      listings: [],
      unlisted_characters: Array.from({ length: chars }, (_, i) => ({ nft_id: 'c' + i, nft_number: 1000 + i, image: null })),
      unlisted_trait_tokens: Array.from({ length: traits }, (_, i) => ({ nft_id: 't' + i, slot: 'Hat', value: 'Hat ' + i, image_url: null })),
      closet_assets: Array.from({ length: closet }, (_, i) => ({ slot: 'Eyes', value: 'Eyes ' + i, count: 1, listed: 0, image_url: null })),
    },
    '/api/market/bids/mine': { my_bids: [], bids_on_my_nfts: [] },
    '/api/closet/orders/mine': { orders: [], bids_on_my_traits: [], fills: [] },
  };
}

const browser = await chromium.launch();
for (const [name, shape] of Object.entries(SHAPES)) {
  for (const width of [900, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, deviceScaleFactor: 2 });
    const data = payloads(shape);
    await page.route('http://lfg.test/**', (route) => {
      const url = new URL(route.request().url());
      const hit = Object.keys(data).find((p) => url.pathname.startsWith(p));
      if (hit) return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data[hit]) });
      const file = url.pathname === '/' ? '/index.html' : url.pathname;
      try {
        const ext = file.split('.').pop();
        return route.fulfill({ contentType: TYPES[ext] || 'application/octet-stream', body: readFileSync(CLIENT + file) });
      } catch { return route.fulfill({ status: 404, body: '' }); }
    });
    await page.goto('http://lfg.test/index.html');
    // Boot only the Mine surface: import the pure module and the renderers
    // directly rather than authenticating the whole Activity.
    await page.evaluate(async (d) => {
      const M = await import('./mine_pure.js?v=1');
      document.querySelector('#market-panel')?.removeAttribute('hidden');
      document.querySelector('#market-mine')?.removeAttribute('hidden');
      window.__built = M.buildMine({ mine: d['/api/market/mine'], bids: d['/api/market/bids/mine'], closet: d['/api/closet/orders/mine'], wallet: 'rMe', closetMarketEnabled: true });
    }, data);
    // renderMine lives in app.js's module scope; drive it through the real
    // client instead if the direct import proves insufficient (see Step 2).
    const box = await page.locator('#market-mine').boundingBox();
    console.log(name, width, 'height', box && Math.round(box.height));
    await page.locator('#market-mine').screenshot({ path: `/tmp/mine-${name}-${width}.png` });
    await page.close();
  }
}
await browser.close();
```

- [ ] **Step 2: Run it, and fall back to the real client if the module-scope renderers aren't reachable**

Run: `node <scratchpad>/mine_shot.mjs`

If `renderMine` cannot be reached from `page.evaluate` (it is module-private in `app.js`), drive the real client instead: serve the same routes, additionally stub `/api/config`, `/api/me` and `/api/nfts` with authenticated fixtures, let `app.js` boot normally, and `page.evaluate` a click on the Mine tab. That is slower but exercises the shipped path; prefer it if it works.

Expected: six PNGs, and printed heights that are **bounded** — the `whale` shape must print a height in the low thousands, not five figures. If a group renders its full list, the cap is not being applied; fix `renderMineGroup` before continuing.

- [ ] **Step 3: Read the screenshots**

Check, at both widths and all three shapes:
- the 1-NFT shape shows **one** group and no italic "nothing here" lines anywhere;
- every action control is at least 40px tall;
- card columns align (no ragged right edge);
- prices render in their own colour with the right unit (XRP on characters, BRIX on traits);
- at 390px the rows stack price and action onto their own full-width lines;
- History is collapsed on load.

Fix anything that fails, re-run, and commit each fix separately.

- [ ] **Step 4: Run the full gate**

Run: `.venv/bin/python -m pytest -q`
Expected: the full suite passes (5,800+ tests). Then run the hooks the pre-push gate runs:
Run: `.venv/bin/python -m pre_commit run --hook-stage pre-push --all-files`
Expected: all hooks pass.

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin feat/mine-redesign
gh pr create --repo Team-Hamsa/LFG \
  --title "feat(mine): five intent groups, cards and rows, bounded height" \
  --body-file - < <scratchpad>/pr-body.md
```

The body must state: the problem (nine flat chip strips, 15,582px for the largest wallet, eight italic apologies for the typical one), the five groups and their order, the caps table, that the server is unchanged, and the measured before/after heights from Step 2. **No AI attribution** — no `Co-Authored-By: Claude` trailer on the PR body, no "Generated with" footer.

- [ ] **Step 6: Babysit the review bots**

Wait for Greptile and CodeRabbit. Greptile's clean verdict lives ONLY in the check run:
```bash
sha=$(gh pr view <n> --repo Team-Hamsa/LFG --json headRefOid --jq .headRefOid)
gh api repos/Team-Hamsa/LFG/commits/$sha/check-runs \
  --jq '.check_runs[]|select(.name|test("reptile"))|{conclusion,summary:.output.summary}'
```
Fix every actionable finding **and reply on its thread** naming the fixing commit:
```bash
gh api -X POST repos/Team-Hamsa/LFG/pulls/<n>/comments/<comment_id>/replies -f body='…'
```
Re-trigger with `@greptile-apps please re-review` / `@coderabbitai review`. If CodeRabbit is rate-limited and Greptile is clean, merge on Greptile alone. Then merge and delete the branch.

- [ ] **Step 7: Link the plan and spec back to the issue**

```bash
gh issue comment 583 --repo Team-Hamsa/LFG --body "Spec: <blob URL at the merge commit>
Plan: <blob URL at the merge commit>"
```

---

## Self-review notes

- **Spec coverage:** five intent groups (T1–T3), merged traits with custody badges and quantity roll-up (T1), two display units (T4 CSS, T5 renderers), collapsible headers with retained open state (T4, T5), caps + Show-all drill-in with search (T5, T6), emptiness rules (T1 `ownsNothing`/`nothingActive`, T5 `details.hidden`), `None` handling (T1), `renderChipList` preserved (T5 Step 5), cache busters in lockstep (T4), no server change (Global Constraints), tests (T1–T3) and visual verification (T7). The spec's "Needs you ordering" and the `matched`/`cancelling` rule are Task 2–3 tests.
- **Route check done:** `GET /api/closet/bid/{order_id}` exists (`handle_closet_bid_status`); there is no GET for an ask, which is fine because only a bid is ever `pending_escrow`. `viewClosetOrder` therefore always calls the bid route.
