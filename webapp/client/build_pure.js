// webapp/client/build_pure.js
// Pure decision logic for the Build (Dressing Room) panel, kept free of
// DOM/network code so it can be executed and unit-tested under Node
// (tests/test_build_pure_js.py) — same split as mint_pure.js/market_pure.js.

// Default GO selection: never land on an unindexed token ("#null · still
// indexing…"). Prefer the first character whose metadata has a body; fall
// back to the first character only when none are indexed yet.
export function pickDefaultCharacter(characters) {
  if (!characters || !characters.length) return null;
  const indexed = characters.find((c) => Boolean(c.body));
  return (indexed || characters[0]).nft_id;
}

// Presentation state for one GO-picker tile.
//   label — '#<edition>' ('#?' while the edition is unknown/unindexed)
//   sub   — body name, or 'indexing…' while metadata is incomplete
//   state — 'active' (currently selected) | 'selectable' | 'indexing'
//           (disabled: no body means every layer fetch would 400)
export function goTileState(char, activeNftId) {
  const indexed = Boolean(char.body);
  return {
    label: `#${char.edition == null ? '?' : char.edition}`,
    sub: indexed ? char.body : 'indexing…',
    // Missing body metadata wins: the picker disables only 'indexing' tiles,
    // so an unindexed GO must never be labeled 'active' (it would be
    // selectable but every layer fetch would 400).
    state: !indexed ? 'indexing' : char.nft_id === activeNftId ? 'active' : 'selectable',
  };
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
