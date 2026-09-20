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
    subtitle: `${role} \u00b7 ${FILL_STATE_TEXT[fill.state] ?? fill.state}`,
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

// Rows arrive newest-first from the server; a copy, ascending, without
// mutating the caller's array.
function byOldest(rows) {
  return [...rows].sort((a, b) => String(a.created_ts ?? '').localeCompare(String(b.created_ts ?? '')));
}

export function buildMine({
  mine = {}, bids = {}, closet = {}, wallet = null, closetMarketEnabled = false,
} = {}) {
  const stuff = {
    characters: unlistedCharacters(mine),
    traits: traits(mine, closetMarketEnabled),
  };
  const liveOrders = closet.orders || [];
  const selling = [
    ...(mine.listings || []).map(listingItem),
    ...liveOrders
      .filter((o) => o.side === 'ask' && o.state !== 'pending_escrow')
      .map((o) => orderItem(o, 'card')),
  ];
  const buying = [
    ...(bids.my_bids || []).map((b) => bidItem(b, { kind: 'cancelBid', label: 'Cancel' })),
    ...liveOrders
      .filter((o) => o.side === 'bid' && o.state !== 'pending_escrow')
      .map((o) => orderItem(o, 'row')),
  ];
  const fills = closet.fills || [];
  // Incoming offers first — money waiting on a decision — grouped by kind
  // before sorting, because ranking XRP against BRIX would be meaningless.
  const needsYou = [
    ...(bids.bids_on_my_nfts || [])
      .map((b) => bidItem(b, { kind: 'acceptBid', label: 'Accept' }))
      .sort(byPriceDesc),
    ...(closet.bids_on_my_traits || []).map(holdingBidItem).sort(byPriceDesc),
    // …then the user's own stuck actions, oldest first — the server returns
    // both lists newest-first, and the thing that has been stuck longest is
    // the one most worth finishing.
    ...byOldest(liveOrders.filter((o) => o.state === 'pending_escrow'))
      .map((o) => ({
        ...orderItem(o, 'row'),
        state: null,
        action: {
          label: 'Finish signing',
          kind: 'resumeClosetOrder',
          payload: { id: o.id, side: o.side },
        },
      })),
    ...byOldest(fills.filter((f) => fillNeedsUser(f, wallet)))
      .map((f) => {
        const row = fillItem(f, wallet);
        return f.state === 'failed'
          ? row
          : { ...row, action: { label: 'Finish paying', kind: 'resumeFill', payload: f } };
      }),
  ];
  const history = fills.filter((f) => !fillNeedsUser(f, wallet)).map((f) => fillItem(f, wallet));
  const active = needsYou.length + selling.length + buying.length;
  // Ownership, not activity: an outgoing bid buys nothing yet and a settled
  // fill is a memory, so neither suppresses "You don't own anything yet".
  // Selling counts because every row there is backed by something held.
  const owned = selling.length + stuff.characters.length + stuff.traits.length;
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
