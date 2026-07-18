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
