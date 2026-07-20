// webapp/client/market_pure.js
// Pure-function helpers for the in-app marketplace panel (#44 Task 10): row
// mapping, filter/sort reducers, badge/label logic, query building, and the
// integer-drops money math behind the royalty disclosure. No DOM, no fetch,
// no globals — every function here is a plain input -> output mapping, kept
// separate from webapp/client/app.js's rendering/event-wiring code so it is
// unit-testable under Node (see tests/test_market_pure_js.py) without a
// browser, mirroring lib/market.ts's pure-function pattern from the Baysed
// baseline (spec §Q8) in vanilla JS.
//
// Money discipline (.superpowers/sdd/global-constraints.md): prices are
// integer drops everywhere; BigInt is used for every drops computation and
// floats are never accepted or produced. xrpToDropsStr/dropsToXrpStr mirror
// lfg_core/market_ops.py's xrp_to_drops_str/drops_to_xrp_str (string in,
// string out, reject anything a float could silently misround).

export const DROPS_PER_XRP = 1000000n;

// XRP's total supply is 100 billion XRP = 1e17 drops — no real price can
// exceed it. Mirrors lfg_core/market_ops.py::MAX_XRP (#130).
export const MAX_DROPS = 100000000000n * DROPS_PER_XRP;

// The ledger's NFT_TRANSFER_FEE = 7000 (units of 1/100,000 -> 7.00%) is the
// marketplace's only fee — see lfg_core/market_ops.py and the design spec's
// Rev 2 correction (which fixed a long-standing units mixup elsewhere in this
// codebase's docs). Fee copy here is ALWAYS "7% / seller nets 93%".
export const ROYALTY_FEE_BPS = 700n; // 7.00%, in basis points (1 bps = 0.01%)
export const ROYALTY_BPS_DENOM = 10000n;

const XRP_RE = /^(\d+)(?:\.(\d+))?$/;

/**
 * Convert a decimal XRP amount string to an integer drops string. Mirrors
 * lfg_core/market_ops.py::xrp_to_drops_str: rejects non-string input
 * (TypeError), and non-numeric strings / values <= 0 / values beyond XRP's
 * 100e9 total supply (MAX_DROPS) / more than 6 decimal places (RangeError)
 * — drops are XRP's atomic unit, nothing finer exists.
 */
export function xrpToDropsStr(xrp) {
  if (typeof xrp !== 'string') {
    throw new TypeError(`xrpToDropsStr requires a string, got ${typeof xrp}`);
  }
  const m = XRP_RE.exec(xrp.trim());
  if (!m) throw new RangeError(`invalid XRP amount: ${JSON.stringify(xrp)}`);
  const whole = m[1];
  const fracRaw = m[2] || '';
  if (fracRaw.length > 6) {
    throw new RangeError('XRP amount must not have more than 6 decimal places');
  }
  const frac = fracRaw.padEnd(6, '0');
  const drops = BigInt(whole) * DROPS_PER_XRP + BigInt(frac || '0');
  if (drops <= 0n) throw new RangeError('XRP amount must be > 0');
  if (drops > MAX_DROPS) throw new RangeError('XRP amount exceeds total supply (100000000000 XRP)');
  return drops.toString();
}

/**
 * Convert an integer drops string to a decimal XRP amount string. Inverts
 * xrpToDropsStr; mirrors lfg_core/market_ops.py::drops_to_xrp_str. Rejects
 * non-string / non-digit-string input.
 */
export function dropsToXrpStr(drops) {
  if (typeof drops !== 'string' || !/^\d+$/.test(drops)) {
    throw new TypeError(`dropsToXrpStr requires a digit string, got ${JSON.stringify(drops)}`);
  }
  const value = BigInt(drops);
  const whole = value / DROPS_PER_XRP;
  const frac = value % DROPS_PER_XRP;
  if (frac === 0n) return whole.toString();
  const fracStr = frac.toString().padStart(6, '0').replace(/0+$/, '');
  return `${whole.toString()}.${fracStr}`;
}

/**
 * Validate a user-entered XRP price string for the list/sell forms without
 * throwing — returns {ok:true, drops} or {ok:false, error} so the UI can
 * show an inline message while the user is still typing.
 */
export function validatePrice(xrpStr) {
  try {
    const drops = xrpToDropsStr(xrpStr);
    return { ok: true, drops };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

/**
 * The integer-drops royalty split for a listing priced at `priceXrp` (a
 * decimal XRP string): seller nets 93%, the collection (NFT issuer) takes
 * 7% via the ledger's own TransferFee on the sale — no broker, no rounding
 * drift (receiveDrops is total - fee, so the two always sum exactly).
 * Every field is a string; never a float.
 */
export function computeRoyalty(priceXrp) {
  const totalDrops = BigInt(xrpToDropsStr(priceXrp));
  const feeDrops = (totalDrops * ROYALTY_FEE_BPS) / ROYALTY_BPS_DENOM;
  const receiveDrops = totalDrops - feeDrops;
  return {
    totalDrops: totalDrops.toString(),
    feeDrops: feeDrops.toString(),
    receiveDrops: receiveDrops.toString(),
    totalXrp: dropsToXrpStr(totalDrops.toString()),
    feeXrp: dropsToXrpStr(feeDrops.toString()),
    receiveXrp: dropsToXrpStr(receiveDrops.toString()),
  };
}

/**
 * No-throw wrapper around computeRoyalty for callers rendering
 * server-provided amounts (#133): a malformed listing price ("1E+1", "",
 * "abc") returns {ok:false, error} for the UI to surface via showError
 * instead of rejecting the async click handler into a dead card.
 * computeRoyalty itself still throws — this mirrors validatePrice.
 */
export function safeComputeRoyalty(priceXrp) {
  try {
    return { ok: true, royalty: computeRoyalty(priceXrp) };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

/**
 * The "You receive X XRP (93% — 7% collection royalty)" disclosure shown on
 * every list/sell screen (spec §Q8). Always says 93%/7% — see
 * .superpowers/sdd/global-constraints.md's fee-copy rule.
 */
export function royaltyDisclosure(priceXrp) {
  const { receiveXrp } = computeRoyalty(priceXrp);
  return `You receive ${receiveXrp} XRP (93% — 7% collection royalty)`;
}

// --- #239: BRIX price helpers (trait listings are BRIX-denominated) ---
// Mirrors lfg_core/market_ops.py's validate_brix_value bounds: > 0, at most
// 6 decimal places, capped at 1e15. Micro-BRIX (1e-6) BigInt math throughout
// — same money discipline as the drops helpers above.

export const MICRO_PER_BRIX = 1000000n;
export const MAX_MICRO_BRIX = 1000000000000000n * MICRO_PER_BRIX; // 1e15 BRIX

/** Convert a decimal BRIX string to integer micro-BRIX (1e-6) string. */
export function brixToMicroStr(brix) {
  if (typeof brix !== 'string') {
    throw new TypeError(`brixToMicroStr requires a string, got ${typeof brix}`);
  }
  const m = XRP_RE.exec(brix.trim());
  if (!m) throw new RangeError(`invalid BRIX amount: ${JSON.stringify(brix)}`);
  const fracRaw = m[2] || '';
  if (fracRaw.length > 6) {
    throw new RangeError('BRIX amount must not have more than 6 decimal places');
  }
  const micro = BigInt(m[1]) * MICRO_PER_BRIX + BigInt(fracRaw.padEnd(6, '0') || '0');
  if (micro <= 0n) throw new RangeError('BRIX amount must be > 0');
  if (micro > MAX_MICRO_BRIX) throw new RangeError('BRIX amount exceeds cap (1e15 BRIX)');
  return micro.toString();
}

/** Inverse of brixToMicroStr: micro-BRIX string -> decimal BRIX string. */
export function microToBrixStr(micro) {
  if (typeof micro !== 'string' || !/^\d+$/.test(micro)) {
    throw new TypeError(`microToBrixStr requires a digit string, got ${JSON.stringify(micro)}`);
  }
  const value = BigInt(micro);
  const whole = value / MICRO_PER_BRIX;
  const frac = value % MICRO_PER_BRIX;
  if (frac === 0n) return whole.toString();
  const fracStr = frac.toString().padStart(6, '0').replace(/0+$/, '');
  return `${whole.toString()}.${fracStr}`;
}

/**
 * No-throw BRIX price validation for the list/sell forms — the BRIX twin of
 * validatePrice. Returns {ok:true, value} (normalized decimal string) or
 * {ok:false, error}.
 */
export function validateBrixPrice(brixStr) {
  try {
    return { ok: true, value: microToBrixStr(brixToMicroStr(brixStr)) };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

/**
 * The "You receive X BRIX (93% — 7% collection royalty)" disclosure for a
 * BRIX-priced trait listing — integer micro-BRIX math, same 93/7 rule as
 * royaltyDisclosure.
 */
export function brixRoyaltyDisclosure(brixStr) {
  const total = BigInt(brixToMicroStr(brixStr));
  const fee = (total * ROYALTY_FEE_BPS) / ROYALTY_BPS_DENOM;
  const receive = microToBrixStr((total - fee).toString());
  return `You receive ${receive} BRIX (93% — 7% collection royalty)`;
}

/**
 * A listing row's display price in its own denomination (#239): trait rows
 * render BRIX, character rows XRP. Falls back to the drops fields for a
 * legacy (pre-BRIX) trait row so the grid never renders "undefined".
 */
export function priceLabel(row) {
  if (row.amount_brix != null) return `${row.amount_brix} BRIX`;
  if (row.amount_xrp != null) return `${row.amount_xrp} XRP`;
  return '';
}

/** 'Character' | 'Trait' badge for a browse/mine listing row. */
export function badgeLabel(row) {
  return row.kind === 'trait' ? 'Trait' : 'Character';
}

/**
 * Shape a raw /api/market/listings (or /mine) row into a view-model for the
 * sticker-card grid: a display title, the kind badge, and pass-through
 * fields the renderer needs. Pure — no DOM node is built here.
 */
export function mapListingRow(row) {
  const isTrait = row.kind === 'trait';
  const title = isTrait
    ? `${row.slot}: ${row.value}`
    : row.nft_number != null
      ? `#${row.nft_number}`
      : row.nft_id;
  return {
    nftId: row.nft_id,
    kind: row.kind,
    badge: badgeLabel(row),
    title,
    image: row.image || null,
    amountXrp: row.amount_xrp ?? null,
    amountBrix: row.amount_brix ?? null, // #239: BRIX price for trait rows
    priceLabel: priceLabel(row),
    seller: row.seller,
    offerIndex: row.offer_index,
    slot: row.slot ?? null,
    value: row.value ?? null,
    nftNumber: row.nft_number ?? null,
    // #131: external (brokered) listings are read-only price discovery.
    // Absent `buyable` (older server / mine rows) defaults to buyable.
    buyable: row.buyable !== false,
    external: row.source === 'external',
    marketplace: row.marketplace ?? null,
    externalUrl: row.external_url ?? null,
  };
}

/**
 * The "Listed on <marketplace>" label for an external listing card (#131);
 * empty string for a normal buyable row.
 */
export function externalLabel(vm) {
  if (!vm.external) return '';
  return vm.marketplace ? `Listed on ${vm.marketplace}` : 'External listing';
}

/**
 * Client-side stable sort for listing rows that don't come pre-sorted from
 * the server (the Mine tab's listings group concatenates two unsorted SQL
 * queries — see lfg_service/app.py::_compute_mine_data). Browse rows are
 * already sorted server-side (handle_market_listings); this is a harmless
 * no-op re-sort there. Unknown `sort` values (or 'newest' — Mine rows carry
 * no created_ts) return the input order unchanged.
 */
export function sortRows(rows, sort) {
  const arr = rows.slice();
  // BigInt compare: amount_drops can exceed Number.MAX_SAFE_INTEGER for large
  // XRP prices, where Number() subtraction would misorder rows (money
  // discipline — integer drops end to end).
  // #239: compare within a row's own denomination — micro-BRIX for trait
  // rows, drops for character rows (browse/Mine groups are per-kind, so a
  // mixed compare only happens for legacy transition rows and stays sane).
  const key = (r) => (r.amount_brix != null ? BigInt(brixToMicroStr(r.amount_brix)) : BigInt(r.amount_drops ?? 0));
  const cmp = (a, b) => {
    const x = key(a);
    const y = key(b);
    return x < y ? -1 : x > y ? 1 : 0;
  };
  if (sort === 'price_asc') {
    arr.sort(cmp);
  } else if (sort === 'price_desc') {
    arr.sort((a, b) => cmp(b, a));
  }
  return arr;
}

/** "Slot:Value" trait-filter query token, matching handle_market_listings's `trait=Slot:Value` parsing. */
export function traitFilterToken(slot, value) {
  return `${slot}:${value}`;
}

/**
 * Build the ordered [key, value] query-param pairs for GET /api/market/listings
 * from a filter-bar state object. Omits any field that is empty/undefined so
 * the request only carries params the user actually set; `traits` (an array
 * of "Slot:Value" tokens) becomes one repeated `trait` param per entry,
 * matching request.query.getall('trait') server-side.
 */
export function buildListingsParams({ kind, traits, minXrp, maxXrp, minBrix, maxBrix, sort, limit, offset, includeExternal } = {}) {
  const pairs = [];
  if (kind) pairs.push(['kind', kind]);
  // #131: opt-in known-broker external (read-only) rows.
  if (includeExternal) pairs.push(['include_external', '1']);
  for (const t of traits || []) pairs.push(['trait', t]);
  if (minXrp !== undefined && minXrp !== null && minXrp !== '') pairs.push(['min_xrp', String(minXrp)]);
  if (maxXrp !== undefined && maxXrp !== null && maxXrp !== '') pairs.push(['max_xrp', String(maxXrp)]);
  // #239: BRIX bounds for trait browse (min_brix/max_brix server params).
  if (minBrix !== undefined && minBrix !== null && minBrix !== '') pairs.push(['min_brix', String(minBrix)]);
  if (maxBrix !== undefined && maxBrix !== null && maxBrix !== '') pairs.push(['max_brix', String(maxBrix)]);
  if (sort) pairs.push(['sort', sort]);
  if (limit !== undefined && limit !== null) pairs.push(['limit', String(limit)]);
  if (offset !== undefined && offset !== null) pairs.push(['offset', String(offset)]);
  return pairs;
}

// --- Trait-sell wizard step labels (spec §Q8's two-signature labeling) ---

export const TRAIT_WIZARD_STEP_LABELS = {
  extract_pending: '1 of 2: claim your trait token',
  extract_done: '1 of 2: claim your trait token',
  list_pending: '2 of 2: post your listing',
  listed: '2 of 2: post your listing',
};

export function traitWizardStepLabel(state) {
  return TRAIT_WIZARD_STEP_LABELS[state] || '';
}

// --- marketFlow's terminal-state check (list/cancel/buy/trait-sell share
// the DONE/FAILED/UNKNOWN vocabulary from lfg_core/market_flow.py, plus the
// trait wizard's own LISTED) ---

export const MARKET_TERMINAL_STATES = new Set(['done', 'failed', 'unknown', 'listed']);

export function isMarketTerminal(state) {
  return MARKET_TERMINAL_STATES.has(state);
}

// Shown when a trait purchase 403s with {"error":"closet_required"} (a buyer
// with no active Closet can't receive the settled trait) — same tone as the
// dressup panel's own Closet gate copy (app.js's "You need a Closet to store
// your traits.").
export const CLOSET_REQUIRED_MESSAGE =
  'You need a Closet to buy traits — claim one first, then come back to complete this purchase.';
