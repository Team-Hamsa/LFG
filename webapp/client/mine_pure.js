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
  return {
    items: items.slice(0, cap),
    total: items.length,
    hidden: Math.max(0, items.length - cap),
  };
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
      action: {
        label: 'List',
        kind: 'list',
        payload: { nftId: c.nft_id, label: title, wizard: false },
      },
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

export function buildMine({
  mine = {}, bids = {}, closet = {}, wallet = null, closetMarketEnabled = false,
} = {}) {
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
