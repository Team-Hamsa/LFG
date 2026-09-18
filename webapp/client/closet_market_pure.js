// webapp/client/closet_market_pure.js
// Closet Market (#443) pure helpers — labels, disclosures and BRIX money math
// for the order book UI. Kept DOM-free so tests/test_closet_market_pure_js.py
// can execute it under Node. BRIX math is BigInt micro-units (6 dp), rounding
// the fee DOWN exactly like closet_market_store.fee_for on the server.

const MICRO = 1000000n;

export function toMicro(s) {
  const [whole, frac = ''] = String(s).split('.');
  return BigInt(whole || '0') * MICRO + BigInt((frac + '000000').slice(0, 6));
}

export function fromMicro(n) {
  const whole = n / MICRO;
  const frac = (n % MICRO).toString().padStart(6, '0').replace(/0+$/, '');
  return frac ? `${whole}.${frac}` : `${whole}`;
}

export function sellerNet(priceBrix, feeBps) {
  const price = toMicro(priceBrix);
  return fromMicro(price - (price * BigInt(feeBps)) / 10000n);
}

function feePct(feeBps) {
  return `${Number(feeBps) / 100}%`;
}

export function askDisclosure(priceBrix, feeBps) {
  const net = sellerNet(priceBrix, feeBps);
  const fee = Number(feeBps) > 0 ? ` after the ${feePct(feeBps)} market fee` : '';
  return `You'll receive ${net} BRIX${fee} when it sells. Listing is free and instant — unlist any time.`;
}

export function bidDisclosure(priceBrix, ttlDays) {
  const amount = priceBrix == null ? 'your BRIX' : `${priceBrix} BRIX`;
  return `Locks ${amount} in an on-ledger escrow until a holder fills it, you cancel, or it expires in ${ttlDays} days. `
    + 'If you match a cheaper listing, the difference comes back to you.';
}

export function fillDisclosure(priceBrix, feeBps, needsDeposit) {
  const net = sellerNet(priceBrix, feeBps);
  const fee = Number(feeBps) > 0 ? ` after the ${feePct(feeBps)} market fee` : '';
  const deposit = needsDeposit ? 'Your trait token is deposited back into your Closet first. ' : '';
  return `${deposit}You'll receive ${net} BRIX${fee}; the trait moves to the buyer's Closet. No signature needed.`;
}

export function mapBookRow(row) {
  const count = (n) => (n > 1 ? ` (${n})` : '');
  return {
    slot: row.slot,
    value: row.value,
    title: `${row.slot}: ${row.value}`,
    imageUrl: row.image_url ?? null,
    bestAsk: row.best_ask_brix ?? null,
    bestBid: row.best_bid_brix ?? null,
    askLabel: row.best_ask_brix == null ? 'No asks' : `Ask ${row.best_ask_brix} BRIX${count(row.ask_count)}`,
    bidLabel: row.best_bid_brix == null ? 'No bids' : `Bid ${row.best_bid_brix} BRIX${count(row.bid_count)}`,
  };
}

export function bookBadge(book) {
  return book && book.best_bid_brix != null ? `Bid ${book.best_bid_brix}` : null;
}

export function bookLine(book) {
  if (!book) return '';
  const parts = [];
  if (book.best_bid_brix != null) parts.push(`best bid ${book.best_bid_brix} BRIX`);
  if (book.best_ask_brix != null) parts.push(`best ask ${book.best_ask_brix} BRIX`);
  return parts.length ? `Closet market: ${parts.join(' · ')}` : '';
}

export function bestAskFor(levels, wallet) {
  const asks = (levels && levels.asks) || [];
  return asks.find((a) => a.owner !== wallet) || null;
}

export function keySlots(keys) {
  return [...new Set(keys.map((k) => k.slot))].sort();
}

export function keyValues(keys, slot) {
  return keys.filter((k) => k.slot === slot).map((k) => k.value).sort();
}

const ORDER_STATE_TEXT = {
  pending_escrow: 'awaiting signature', open: 'open', matched: 'filling', cancelling: 'cancelling',
  filled: 'filled', cancelled: 'cancelled', expired: 'expired',
};

export function orderChipLabel(order) {
  const side = order.side === 'bid' ? 'Bid' : 'Ask';
  return `${side} · ${order.slot}: ${order.value} · ${order.price_brix} BRIX (${ORDER_STATE_TEXT[order.state] ?? order.state})`;
}

const FILL_STATE_TEXT = {
  funds_pending: 'Waiting for funds', funded: 'Moving the trait', asset_moved: 'Paying the seller',
  paid: 'Updating Closets', mirrored: 'Complete', refund_pending: 'Refunding', refunded: 'Refunded',
  indeterminate: 'Confirming on-ledger', failed: 'Failed',
};

export function fillStateText(state) {
  return FILL_STATE_TEXT[state] ?? state;
}

export function fillChipLabel(fill, wallet) {
  const role = fill.buyer === wallet ? 'Bought' : 'Sold';
  return `${role} · ${fill.slot}: ${fill.value} · ${fill.price_brix} BRIX — ${fillStateText(fill.state)}`;
}

export function holdingFillAction(entry) {
  return entry.source === 'token'
    ? { label: 'Deposit & fill', needsDeposit: true, nftId: entry.nft_id }
    : { label: 'Fill', needsDeposit: false, nftId: null };
}

export function holdingLabel(entry) {
  const where = entry.source === 'token' ? ' (in your wallet)' : '';
  return `${entry.slot}: ${entry.value} — ${entry.price_brix} BRIX${where}`;
}

export function closetAssetLabel(asset) {
  const listed = asset.listed ? ` (${asset.listed} listed)` : '';
  return `${asset.slot}: ${asset.value} ×${asset.count}${listed}`;
}
