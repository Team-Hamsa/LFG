// webapp/client/build_pure.js
// Pure decision logic for the Build (Dressing Room) panel, kept free of
// DOM/network code so it can be executed and unit-tested under Node
// (tests/test_build_pure_js.py) — same split as mint_pure.js/market_pure.js.

// Default GO selection: never land on an unindexed token ("#null · still
// indexing…"). Prefer the first DRESSED (non-blank) indexed character so the
// canvas opens on something to look at rather than a bare silhouette; fall
// back to the first indexed character (which may be a blank), then to the
// first character only when none are indexed yet.
export function pickDefaultCharacter(characters) {
  if (!characters || !characters.length) return null;
  const dressed = characters.find((c) => Boolean(c.body) && !c.blank);
  if (dressed) return dressed.nft_id;
  const indexed = characters.find((c) => Boolean(c.body));
  return (indexed || characters[0]).nft_id;
}

// Presentation state for one GO-picker tile.
//   label — '#<edition>' ('#?' while the edition is unknown/unindexed)
//   sub   — body name, or 'indexing…' while metadata is incomplete
//   state — 'active' (currently selected) | 'selectable' | 'indexing'
//           (disabled: no body means every layer fetch would 400)
export function goTileState(char, activeNftId) {
  // A blank wears no Body, yet it is fully indexed: renderCanvas draws its own
  // silhouette image and fetches no layers, so the "no body -> every layer
  // fetch would 400" disable must not apply to it. Without this, every blank
  // rendered as a disabled "indexing…" tile and could never be selected to be
  // rebuilt. Note `char.body` is NOT how you recognize a blank (#523) — it is a
  // body CLASS, and detect_body() falls through to "skeleton" for a blank's
  // all-"None" attributes rather than returning ''; only `blank` says so.
  const indexed = Boolean(char.body) || Boolean(char.blank);
  return {
    label: `#${char.edition == null ? '?' : char.edition}`,
    sub: char.blank ? 'Blank — build me!' : indexed ? char.body : 'indexing…',
    // Missing body metadata wins: the picker disables only 'indexing' tiles,
    // so an unindexed GO must never be labeled 'active' (it would be
    // selectable but every layer fetch would 400).
    state: !indexed ? 'indexing' : char.nft_id === activeNftId ? 'active' : 'selectable',
  };
}

// Which blank the Assemble builder opens on. `preselectNftId` is set when the
// user came from a specific blank in the Build panel ("Build this GO"); an
// unknown id (a blank the server did not return, e.g. a non-mutable legacy
// character) falls back to the normal rule: auto-select a lone blank, else let
// the user pick in step 1.
export function pickBuilderBlank(blanks, preselectNftId) {
  const list = blanks || [];
  const pre = preselectNftId ? list.find((b) => b.nft_id === preselectNftId) : null;
  if (pre) return pre;
  return list.length === 1 ? list[0] : null;
}

// --- Pending (unsaved) Build changes -----------------------------------
// The Build panel stages tile clicks in a `{slot: incomingValue}` map and only
// commits them on Save, as ONE NFTokenModify. These three functions are the
// whole model: what the canvas draws, what the Closet grid shows, and what the
// POST body is.

// The character's attributes with every staged change applied. Slots the
// character does not have are ignored (never invented).
export function applyPending(attributes, pending) {
  const staged = pending || {};
  return (attributes || []).map((a) => (
    Object.prototype.hasOwnProperty.call(staged, a.trait_type)
      ? { ...a, value: staged[a.trait_type] }
      : a
  ));
}

// Current value held in `slot` by `character`; 'None' when the slot is empty or
// the character has no such attribute — the same convention the server's
// trait_economy.slot_value uses.
function currentValue(character, slot) {
  if (!character) return 'None';
  const a = (character.attributes || []).find((x) => x.trait_type === slot);
  return (a && a.value) || 'None';
}

// Closet counts with the staged changes applied: each staged incoming asset is
// -1, each displaced value is +1. Entries reaching 0 are dropped; a displaced
// value the Closet did not already hold is synthesized so it can be clicked
// back on. 'None' is never materialized as a tile (it is the file-less
// stand-in for an empty slot, not a real asset).
export function effectiveAssets(assets, character, pending) {
  const staged = pending || {};
  const out = (assets || []).map((a) => ({ ...a }));
  if (!character) return out;
  const find = (slot, value) => out.find((a) => a.slot === slot && a.value === value);
  for (const slot of Object.keys(staged)) {
    const incoming = staged[slot];
    const displaced = currentValue(character, slot);
    if (incoming === displaced) continue;       // staged back to current: no-op
    const inEntry = find(slot, incoming);
    if (inEntry) inEntry.count -= 1;
    if (displaced !== 'None') {
      const outEntry = find(slot, displaced);
      if (outEntry) outEntry.count += 1;
      else out.push({ slot, value: displaced, count: 1 });
    }
  }
  return out.filter((a) => a.count > 0);
}

// The `changes` array for POST /api/equip: one {slot, value} per staged slot
// whose value actually differs from what the character wears on-chain. A slot
// staged back to its current value nets out — that is how undo works.
export function netChanges(character, pending) {
  const staged = pending || {};
  if (!character) return [];
  return Object.keys(staged)
    .filter((slot) => staged[slot] !== currentValue(character, slot))
    .map((slot) => ({ slot, value: staged[slot] }));
}

// Can this loose Closet asset be equipped onto the active character? A BLANK is
// never equippable (#523): dressing one slot leaves a partly dressed character
// and credits a phantom "None" to every other slot, which the conservation
// audit reads as drift — Assemble ("Build this GO") is the only supported way
// to dress a blank. `blank` is the ONLY field that says so. A blank's `body` is
// a body CLASS, not '' — detect_body() has no "no body" answer and falls
// through to "skeleton" for its all-"None" attributes — so neither a body check
// nor closetTileState()'s visibility can stand in for this.
export function equipCompatible(character, asset, slots) {
  if (!character || character.blank) return false;
  return (slots || []).includes(asset && asset.slot);
}

// Presentation state for one Closet tile (asset {slot, value}) given the
// selected GO (`char`, or null when none is selected).
//   visible — false only when the value's art can't render on this body (it
//             reappears under a GO whose body does have the art)
//   art     — 'layer' (fetch the body-specific layer file) | 'blank'
//   label   — text to show in place of art
//
// "None" is a real, conserved asset — a harvest deposits one per empty slot,
// and equipping it is how a slot gets cleared — so a "None" tile is ALWAYS
// visible. It just has nothing to draw (its art is an empty image, and some
// bodies have no None file at all, whose 404 used to delete the tile), hence
// a labeled blank placeholder rather than a layer fetch.
export function closetTileState(asset, char) {
  const value = asset && asset.value;
  if (!value || value === 'None') return { visible: true, art: 'blank', label: 'None' };
  if (!char) return { visible: true, art: 'blank', label: '' };
  if (!char.body) return { visible: false, art: 'blank', label: '' };
  return { visible: true, art: 'layer', label: '' };
}

// The keyboard equivalent of a click. A <div role="button"> — which is what a
// Closet tile has to be, since it wraps the nested Extract <button> — gets
// none of a native button's behavior for free: no Enter/Space activation, and
// the client has no global key delegation (every keydown listener in app.js
// closes an overlay on Escape). So the tile has to recognize these itself.
export function isActivationKey(key) {
  return key === 'Enter' || key === ' ';
}

// Widget semantics for one Closet tile, given whether its asset can be
// equipped on the selected GO. A tile is presented as a button only while it
// IS one: the incompatible case wires no equip handler, so it claims no role
// and takes no tab stop — a focus stop that does nothing while announcing
// itself as a button is the bug being fixed, not the fix.
//
// It deliberately does not get aria-disabled instead. Per ARIA that state
// extends to the focusable descendants of the element carrying it, and this
// tile contains the Extract button, which stays usable whatever the equip
// compatibility — announcing Extract as disabled would be a worse lie than
// saying nothing about equip (PR #533 review). What is left is art, a count,
// and a normally-announced Extract button; the dimmed .incompatible styling
// carries the visual half.
export function closetTileA11y(compatible) {
  return compatible ? { role: 'button', tabIndex: 0 } : { role: null, tabIndex: null };
}

// Which tile takes focus once staging an equip has rebuilt the grid. `keys`
// are the rebuilt focusable tiles' "<slot>:<value>" identities, `key` the
// activated one, `previousIndex` the position it held. The same tile when it
// survived (staging merely dropped its count), otherwise whatever slid into
// its place — clamped to the end of a shrunken grid — and -1 when nothing is
// left to focus.
export function restoredTileIndex(keys, key, previousIndex) {
  const list = keys || [];
  if (!list.length) return -1;
  const exact = list.indexOf(key);
  if (exact !== -1) return exact;
  return Math.min(Math.max(previousIndex, 0), list.length - 1);
}

// First legal value per slot from an options map — mirrors the server's old
// first-match prefill so one-tap assemble still works.
export function defaultChosen(slots, slotOptions) {
  const chosen = {};
  for (const s of slots) {
    const vals = slotOptions[s] || [];
    if (vals.length) chosen[s] = vals[0];
  }
  return chosen;
}

// Slots with no legal closet asset for this body (blocks the commit button).
export function missingSlots(slots, slotOptions) {
  return slots.filter((s) => !(slotOptions[s] || []).length);
}

// --- Layered preview (T31 defect 1) -------------------------------------
// The single stacking order every layered preview (Dressing Room canvas,
// Assemble builder) must use. `order` is economyState.trait_order (mirrors
// swap_meta.TRAIT_ORDER server-side — Body sits between Back and Clothing,
// NOT painted first); `valuesBySlot` is a {slot: value} map that must
// include "Body" alongside the trait slots. Returns the [{slot, value}, ...]
// pairs to actually draw, bottom layer first, dropping any slot that is
// unset or explicitly "None". A caller that force-prepends Body (or
// otherwise re-derives its own order) is a second, divergent stacker.
//
// `zOrder` is economyState.z_order (TraitConfig.z_table(): each layer's z
// plus trait_config.yaml's per-(trait_type, value) z_overrides). The layers
// are sorted by it the way the server's sort_attributes composes the real
// art, so e.g. Wavy Eyes (z 95) draws above Head/Accessory and Retardio
// (z 45) below Mouth. Equal z keeps `order`: the server stably sorts
// attributes already normalized to trait_order. Without a table that keys
// every drawn layer (a client newer than the API it talks to), the fixed
// `order` stands.
export function orderedLayers(order, valuesBySlot, zOrder) {
  const values = valuesBySlot || {};
  const layers = (order || [])
    .map((slot) => ({ slot, value: values[slot] }))
    .filter(({ value }) => Boolean(value) && value !== 'None');
  const zs = layers.map(({ slot, value }) => layerZ(zOrder, slot, value));
  if (!zs.every(Number.isFinite)) return layers;
  return layers
    .map((layer, i) => ({ layer, z: zs[i], i }))
    .sort((a, b) => a.z - b.z || a.i - b.i)
    .map(({ layer }) => layer);
}

// TraitConfig.z_for, line for line: the FIRST z_override matching both slot
// and value wins, else the slot's own layer z. Anything that is not a finite
// number (no table, unknown slot) makes orderedLayers keep the fixed order.
function layerZ(zOrder, slot, value) {
  if (!zOrder) return undefined;
  for (const o of zOrder.z_overrides || []) {
    if (o.trait_type === slot && o.value === value) return o.z;
  }
  return (zOrder.layers || {})[slot];
}

// --- Body switch keeps valid traits (T31 defect 2) -----------------------
// `slotOptions` is opts.options[newBody] from /api/assemble/options — the
// server's own body-affinity-filtered legal-value list per slot (the SAME
// swap_compose.resolve_layer cross-body matrix check the equip/swap paths
// enforce, see economy_api._require_body_affinity). A previously staged
// value that still appears there survives untouched; one that does not is
// reported in `dropped` and replaced by that slot's default (the same
// first-legal-value rule defaultChosen applies to a fresh pick), so `chosen`
// stays a complete map matching what every <select> visibly shows. A slot
// that was never chosen is never reported as dropped.
export function carryOverChosen(slots, slotOptions, previousChosen) {
  const prev = previousChosen || {};
  const fallback = defaultChosen(slots, slotOptions);
  const chosen = {};
  const dropped = [];
  for (const slot of slots) {
    const legal = slotOptions[slot] || [];
    const prevVal = prev[slot];
    if (prevVal != null && legal.includes(prevVal)) {
      chosen[slot] = prevVal;
      continue;
    }
    if (prevVal != null) dropped.push(slot);
    if (fallback[slot] != null) chosen[slot] = fallback[slot];
  }
  return { chosen, dropped };
}

// User-facing summary for carryOverChosen's `dropped` list; null when
// nothing was dropped (no notice to show).
export function dropNoticeText(dropped) {
  const n = (dropped || []).length;
  if (!n) return null;
  const noun = n === 1 ? 'trait' : 'traits';
  const verb = n === 1 ? "doesn't" : "don't";
  const was = n === 1 ? 'was' : 'were';
  return `${n} ${noun} ${verb} fit this body and ${was} cleared.`;
}

// --- Pickers collapse after a choice (T31 defect 3) -----------------------
// Disclosure state for a Builder step (pick a blank / pick a body): expanded
// while nothing is picked yet (there is nothing to collapse to), or when the
// user explicitly forced it back open to change their pick; collapsed to a
// one-line summary once a pick exists and nobody forced it open.
export function stepExpanded(picked, forcedOpen) {
  return Boolean(forcedOpen) || !picked;
}

// #316: classify a terminal equip session for the Build save UX.
// "committed"  — new traits are on-ledger; apply the save locally.
// "uncertain"  — outcome unknown (equip_sync_indeterminate / failed_revert);
//                do not trust an index redraw, gate re-saves until a refresh.
// "reverted"   — everything else (clean failure; character unchanged).
export function saveOutcome(s) {
  if (!s) return 'reverted';
  if (s.resolution === 'uncertain') return 'uncertain';
  if (s.state === 'done' || s.resolution === 'committed') return 'committed';
  return 'reverted';
}
