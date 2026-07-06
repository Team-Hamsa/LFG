// LFG Discord Activity frontend.
//
// Inside Discord the page is served through the Activity proxy; the SDK is
// vendored same-origin at vendor/embedded-app-sdk.js (see docs/ACTIVITY_SETUP.md).
// Outside Discord (no frame_id query param) it runs in a degraded dev mode
// without Discord auth, so the API will return 401 — useful only for UI work.

// Marketplace (#44 Task 10) pure-function helpers: row mapping, filter/sort,
// money math, and wizard-step labels. Kept in a separate module so they're
// unit-testable under Node (tests/test_market_pure_js.py) without a browser
// — see webapp/client/market_pure.js's own header for the full rationale.
import * as marketPure from './market_pure.js';

const params = new URLSearchParams(window.location.search);
const insideDiscord = params.has('frame_id');
// Telegram injects a signed launch payload as Telegram.WebApp.initData; the
// vendored telegram-web-app.js (loaded before this module) defines window.Telegram
// inside Telegram and stays undefined everywhere else.
const tg = window.Telegram && window.Telegram.WebApp;
const insideTelegram = !!(tg && tg.initData);

const el = (id) => document.getElementById(id);
const status = (msg) => { el('status').textContent = msg; };

// Errors surface as dismissing toasts instead of easily-missed status text.
function toast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.setAttribute('role', 'alert');
  t.textContent = msg;
  el('toasts').appendChild(t);
  setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 350);
  }, 4500);
}

function showError(msg) {
  toast(msg);
  status('');
}

// Discord serves the Activity in a sandboxed iframe where native window.confirm
// is a silent no-op (returns false), so confirmations use an in-app overlay.
// Returns a Promise<boolean> that resolves true only when the user confirms.
function confirmDialog({ title, text, confirmLabel = 'Confirm' }) {
  const overlay = el('confirm-overlay');
  if (!overlay.hidden) return Promise.resolve(false); // a dialog is already open
  el('confirm-title').textContent = title;
  el('confirm-text').textContent = text || '';
  el('confirm-ok').textContent = confirmLabel;
  overlay.hidden = false;
  return new Promise((resolve) => {
    const onKey = (e) => { if (e.key === 'Escape') close(false); }; // ARIA: Esc cancels
    const close = (result) => {
      overlay.hidden = true;
      el('confirm-ok').onclick = null;
      el('confirm-cancel').onclick = null;
      overlay.onclick = null;
      document.removeEventListener('keydown', onKey);
      resolve(result);
    };
    el('confirm-ok').onclick = () => close(true);
    el('confirm-cancel').onclick = () => close(false);
    overlay.onclick = (e) => { if (e.target === overlay) close(false); }; // backdrop = cancel
    document.addEventListener('keydown', onKey);
  });
}

let sessionToken = null;
let me = null;
let pollTimer = null;
let externalOpener = null; // set when the SDK is available

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (sessionToken) headers['Authorization'] = `Bearer ${sessionToken}`;
  const res = await fetch(path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function qrUrl(data) {
  return `/api/qr.png?d=${encodeURIComponent(data)}`;
}

// CDN images are cross-origin and blocked by the Activity's CSP, so they are
// routed through the backend's same-origin proxy (like the QR codes).
function imgUrl(url) {
  return url ? `/api/img?u=${encodeURIComponent(url)}` : url;
}

// Guild/channel hosting the Activity; the backend turns these into a XUMM
// return_url so Xaman's post-sign button bounces back into Discord.
function discordCtx() {
  return {
    guild_id: params.get('guild_id'),
    channel_id: params.get('channel_id'),
  };
}

function openExternal(url) {
  if (externalOpener) externalOpener(url);
  else window.open(url, '_blank');
}

async function setupDiscord() {
  // SDK is vendored same-origin (webapp/client/vendor/) to avoid esm.sh's
  // root-absolute re-exports, which break under the Activity's /.proxy sub-path.
  const { DiscordSDK, Common } = await import('./vendor/embedded-app-sdk.js');
  const { client_id: clientId } = await api('/api/config');
  const sdk = new DiscordSDK(clientId);
  await sdk.ready();

  // Follow device orientation instead of Discord's landscape default (#13).
  // Mobile-only command: ignore the rejection on desktop / older clients.
  try {
    const unlocked = Common.OrientationLockStateTypeObject.UNLOCKED;
    await sdk.commands.setOrientationLockState({
      lock_state: unlocked,
      picture_in_picture_lock_state: unlocked,
      grid_lock_state: unlocked,
    });
  } catch (e) { /* not supported here */ }

  const { code } = await sdk.commands.authorize({
    client_id: clientId,
    response_type: 'code',
    state: '',
    prompt: 'none',
    scope: ['identify'],
  });

  const tokenData = await api('/api/token', {
    method: 'POST',
    body: JSON.stringify({ code }),
  });
  sessionToken = tokenData.session_token;

  await sdk.commands.authenticate({ access_token: tokenData.access_token });
  externalOpener = (url) => sdk.commands.openExternalLink({ url });
  return tokenData.user;
}

// Telegram Mini App handshake (#89): validate the signed initData server-side,
// store the returned platform="telegram" session token the same way the Discord
// path stores its token, then run the IDENTICAL UI.
async function setupTelegram() {
  tg.ready();
  tg.expand(); // use the full available height
  const tokenData = await api('/api/telegram/auth', {
    method: 'POST',
    body: JSON.stringify({ init_data: tg.initData }),
  });
  sessionToken = tokenData.session_token;
  externalOpener = (url) => tg.openLink(url);
  return tokenData.user;
}

const ALL_PANELS = ['register-panel', 'mint-panel', 'flow-panel',
                    'swap-panel', 'swap-traits-panel', 'swap-result-panel',
                    'dressup-panel', 'market-panel', 'market-list-form-panel'];

function showPanel(id) {
  for (const panel of ALL_PANELS) {
    el(panel).hidden = panel !== id;
  }
}

function showMintHome() {
  el('wallet-display').textContent = me.wallet;
  showPanel('mint-panel');
  status(`Hey ${me.username} — welcome to the job site.`);
  loadLeaderboard();
}

// --- Leaderboard (home-screen card) ---

const STEPPED_PERIODS = ['week', 'month', 'year'];
const NFT_BOARDS = ['nft_swaps', 'nft_rarity'];
// Two-tier board selector: category tabs → sub-board chips. The sub-row is
// rendered from this map so HTML and JS can't drift. Board keys match the
// /api/leaderboard contract and are unchanged.
const CATEGORIES = {
  users: [
    { board: 'users_nfts', label: 'Holders' },
    { board: 'users_swaps', label: 'Swappers' },
    { board: 'users_builds', label: 'Builders' },
  ],
  nfts: [
    { board: 'nft_swaps', label: 'Swaps' },
    { board: 'nft_rarity', label: 'Rarest' },
  ],
  brix: [
    { board: 'brix_rich', label: 'Richlist' },
    { board: 'brix_lp', label: 'LP' },
    { board: 'brix_earned', label: 'Earned' },
  ],
};
const lbState = { period: 'week', cat: 'users', board: 'users_nfts', anchor: null };

function renderLbBoards() {
  const row = el('lb-boards');
  row.replaceChildren(
    ...CATEGORIES[lbState.cat].map(({ board, label }) => {
      const btn = document.createElement('button');
      btn.className = 'lb-chip';
      btn.setAttribute('role', 'tab');
      btn.dataset.board = board;
      const active = board === lbState.board;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', String(active));
      btn.textContent = label;
      return btn;
    })
  );
}
const numberFmt = new Intl.NumberFormat();

// Anchor date math: returns the ISO (YYYY-MM-DD, UTC) start of the
// previous/next period relative to `anchor` (or today when anchor is null).
function stepAnchor(period, anchor, dir) {
  const base = anchor ? new Date(`${anchor}T00:00:00Z`) : new Date();
  let d;
  if (period === 'week') {
    d = new Date(Date.UTC(base.getUTCFullYear(), base.getUTCMonth(), base.getUTCDate()));
    d.setUTCDate(d.getUTCDate() + dir * 7);
  } else if (period === 'month') {
    const y = base.getUTCFullYear();
    const m = base.getUTCMonth();
    d = new Date(Date.UTC(y, m + dir, 1));
  } else if (period === 'year') {
    const y = base.getUTCFullYear();
    d = new Date(Date.UTC(y + dir, 0, 1));
  }
  return d.toISOString().slice(0, 10);
}

function medal(rank) {
  if (rank === 1) return '🥇';
  if (rank === 2) return '🥈';
  if (rank === 3) return '🥉';
  return `#${rank}`;
}

function renderLbRow(row, isNftBoard) {
  const li = document.createElement('li');
  li.className = 'lb-row';
  const rank = document.createElement('span');
  rank.className = 'lb-rank';
  rank.textContent = medal(row.rank);
  const label = document.createElement('span');
  label.className = 'lb-label';
  if (isNftBoard && row.image) {
    const img = document.createElement('img');
    img.className = 'lb-thumb';
    img.src = imgUrl(row.image);
    img.alt = '';
    label.appendChild(img);
  }
  const name = document.createElement('span');
  name.textContent = isNftBoard
    ? (row.display_name || (row.nft_number != null ? `#${row.nft_number}` : '—'))
    : (row.display_name || row.wallet || '—');
  label.appendChild(name);
  const value = document.createElement('span');
  value.className = 'lb-value';
  value.textContent = numberFmt.format(row.value);
  li.replaceChildren(rank, label, value);
  return li;
}

function highlightChips(containerId, dataKey, activeValue) {
  for (const btn of el(containerId).querySelectorAll('.lb-chip')) {
    const active = btn.dataset[dataKey] === activeValue;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', String(active));
  }
}

async function loadLeaderboard() {
  // Chip active states reflect current selection.
  highlightChips('lb-periods', 'period', lbState.period);
  highlightChips('lb-cats', 'cat', lbState.cat);
  highlightChips('lb-boards', 'board', lbState.board);

  const stepper = el('lb-stepper');
  const stepped = STEPPED_PERIODS.includes(lbState.period);
  stepper.hidden = !stepped;
  if (stepped) {
    el('lb-range').textContent = lbState.anchor || 'Current';
    el('lb-next').disabled = !lbState.anchor;
  }

  const list = el('lb-list');
  const empty = el('lb-empty');
  empty.hidden = true;
  list.replaceChildren();

  try {
    const wallet = me && me.wallet ? me.wallet : '';
    const qs = new URLSearchParams({ board: lbState.board, period: lbState.period, me: wallet });
    if (lbState.anchor) qs.set('start', lbState.anchor);
    const data = await api(`/api/leaderboard?${qs.toString()}`);
    const isNftBoard = NFT_BOARDS.includes(lbState.board);
    if (!data.rows || !data.rows.length) {
      empty.textContent = 'Nothing here yet for this period.';
      empty.hidden = false;
    } else {
      list.replaceChildren(...data.rows.map((row) => renderLbRow(row, isNftBoard)));
    }

    const meEl = el('lb-me');
    const inTop = data.me && data.rows && data.rows.some((r) => r.rank === data.me.rank);
    if (data.me && !inTop) {
      meEl.hidden = false;
      meEl.textContent = `You: ${medal(data.me.rank)} — ${numberFmt.format(data.me.value)}`;
    } else {
      meEl.hidden = true;
    }
  } catch (e) {
    list.replaceChildren();
    el('lb-me').hidden = true;
    empty.textContent = 'Leaderboard unavailable.';
    empty.hidden = false;
  }
}

function setupLeaderboard() {
  el('lb-periods').addEventListener('click', (e) => {
    const btn = e.target.closest('.lb-chip');
    if (!btn) return;
    lbState.period = btn.dataset.period;
    lbState.anchor = null;
    loadLeaderboard();
  });
  renderLbBoards();
  el('lb-cats').addEventListener('click', (e) => {
    const btn = e.target.closest('.lb-chip');
    if (!btn || btn.dataset.cat === lbState.cat || !CATEGORIES[btn.dataset.cat]) return;
    lbState.cat = btn.dataset.cat;
    lbState.board = CATEGORIES[lbState.cat][0].board;
    renderLbBoards();
    loadLeaderboard();
  });
  el('lb-boards').addEventListener('click', (e) => {
    const btn = e.target.closest('.lb-chip');
    if (!btn) return;
    lbState.board = btn.dataset.board;
    loadLeaderboard();
  });
  el('lb-prev').addEventListener('click', () => {
    lbState.anchor = stepAnchor(lbState.period, lbState.anchor, -1);
    loadLeaderboard();
  });
  el('lb-next').addEventListener('click', () => {
    if (!lbState.anchor) return;
    const next = stepAnchor(lbState.period, lbState.anchor, 1);
    const today = new Date().toISOString().slice(0, 10);
    lbState.anchor = next >= today ? null : next;
    loadLeaderboard();
  });
}

// Mint flow step indicator (hidden for flows without a stage, e.g. trustlines)
const MINT_STEPS = ['Pay', 'Build', 'Mint', 'Claim'];
const STAGE_STEP = { awaiting_payment: 0, generating: 1, minting: 2, creating_offer: 2, offer_ready: 3 };

function renderSteps(stage) {
  const ol = el('flow-steps');
  if (!(stage in STAGE_STEP)) { ol.hidden = true; return; }
  const active = STAGE_STEP[stage];
  const finished = stage === 'offer_ready';
  ol.hidden = false;
  ol.replaceChildren(...MINT_STEPS.map((name, i) => {
    const li = document.createElement('li');
    li.textContent = name;
    if (finished || i < active) li.className = 'done';
    else if (i === active) li.className = 'active';
    return li;
  }));
}

function showFlow({ title, text, qrData, link, image, done, stage, spinner, celebrate, pill, regen }) {
  showPanel('flow-panel');
  renderSteps(stage);
  el('pay-method').hidden = !pill;
  if (pill) {
    el('pay-pill').className = `pill ${pill.kind}`;
    el('pay-pill').textContent = pill.text;
  }
  el('flow-title').textContent = title;
  el('flow-text').textContent = text || '';
  el('flow-spinner').hidden = !spinner;
  el('flow-qr').hidden = !qrData;
  if (qrData) el('flow-qr').src = qrUrl(qrData);
  el('flow-link-btn').hidden = !link;
  if (link) el('flow-link-btn').onclick = () => openExternal(link);
  el('nft-image').hidden = !image;
  // The minted NFT is the hero: with an image on screen the QR drops to a
  // compact companion size (issue #22).
  el('flow-panel').classList.toggle('with-image', !!image);
  el('nft-image').classList.toggle('celebrate', !!(image && celebrate));
  if (image) el('nft-image').src = image;
  el('flow-regen-btn').hidden = !regen;
  el('flow-done-btn').hidden = !done;
}

// The pay screen adapts to the backend's silently-detected payment path:
// LFGO holders pay LFGO, everyone else pays XRP. Only the pill and the
// price differ — the mechanics are never explained.
function mintPayView(s) {
  const xrp = s.pay_with === 'XRP';
  const pill = { kind: xrp ? 'xrp' : 'lfgo', text: `Paying with ${xrp ? 'XRP' : 'LFGO'}` };
  // QR already scanned: drop it and show a spinner while Xaman finishes (issue #22)
  if (s.qr_scanned) {
    return {
      title: '📲 Approve in Xaman',
      text: 'QR scanned — approve the payment in Xaman and hang tight here.',
      pill,
      spinner: true,
      stage: s.state,
    };
  }
  return {
    title: '💰 Pay to build',
    text: xrp
      ? `Pay ${s.pay_amount} XRP to mint your avatar — no trustline needed. Scan with Xaman, approve, and hang tight here.`
      : `Pay ${s.pay_amount || 1} LFGO — burned on mint. Scan with Xaman, approve, and hang tight here.`,
    pill,
    qrData: s.payment_link,
    link: s.payment_link,
    stage: s.state,
    regen: true,
  };
}

const STAGE_TEXT = {
  generating: ['🎨 Building your avatar', "Payment's in. Laying the bricks on your one-of-a-kind build…"],
  minting: ['⛏️ Minting on XRPL', 'Stamping your build onto the ledger…'],
  creating_offer: ['📨 Creating transfer offer', 'Almost there — preparing the offer to your wallet…'],
};

// Chained setTimeout (not setInterval) so a slow response can never overlap
// the next request or apply stale state out of order.
function pollMint(sessionId) {
  clearTimeout(pollTimer);
  const tick = async () => {
    if (el('flow-panel').hidden) return; // user navigated away
    let s;
    try {
      s = await api(`/api/mint/${sessionId}`);
    } catch (e) {
      pollTimer = setTimeout(tick, 3000); // transient; keep polling
      return;
    }

    if (s.state === 'offer_ready') {
      if (s.accept_signed) {
        showFlow({
          title: `🎉 #${s.nft_number} claimed!`,
          text: 'The transfer is signed — your new avatar is heading to your wallet. Welcome to the job site.',
          image: imgUrl(s.image_url),
          done: true,
          stage: s.state,
          celebrate: true,
        });
        return;
      }
      showFlow({
        title: `🎉 Minted! #${s.nft_number} is yours`,
        text: s.accept_scanned
          ? 'Approve the transfer in Xaman to claim it to your wallet…'
          : 'Scan to accept the transfer and claim it to your wallet. Welcome to the job site.',
        qrData: s.accept_scanned ? null : s.accept_deeplink,
        spinner: s.accept_scanned,
        link: s.accept_deeplink,
        image: imgUrl(s.image_url),
        done: true,
        stage: s.state,
        celebrate: true,
      });
      pollTimer = setTimeout(tick, 3000); // keep watching for the accept signature
      return;
    }
    if (s.state === 'payment_timeout') {
      showFlow({ title: '⏰ Payment timed out', text: 'No payment came through in time. Give it another go.', done: true });
      return;
    }
    if (s.state === 'failed') {
      showFlow({ title: '❌ Mint failed', text: s.error || 'Something went wrong.', done: true });
      return;
    }

    if (s.state === 'awaiting_payment') {
      showFlow(mintPayView(s));
    } else if (STAGE_TEXT[s.state]) {
      const [title, text] = STAGE_TEXT[s.state];
      showFlow({ title, text, stage: s.state, spinner: true });
    }
    pollTimer = setTimeout(tick, 3000);
  };
  pollTimer = setTimeout(tick, 3000);
}

let currentMintId = null;

async function startMint() {
  try {
    const s = await api('/api/mint', { method: 'POST', body: JSON.stringify(discordCtx()) });
    currentMintId = s.id;
    showFlow(mintPayView(s));
    pollMint(s.id);
  } catch (e) {
    showError(e.message);
  }
}

// Missed the QR before it expired? Mint a fresh payment payload without
// restarting the whole session (issue #22).
async function regeneratePaymentQr() {
  if (!currentMintId) return;
  const btn = el('flow-regen-btn');
  btn.disabled = true;
  try {
    const s = await api(`/api/mint/${currentMintId}/regenerate`, {
      method: 'POST', body: JSON.stringify(discordCtx()),
    });
    showFlow(mintPayView(s));
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
  }
}

// --- Registration via Xaman Sign In (issue #24) ---

let signinPollTimer = null;

function renderSignin({ sub, spinner, qrLink, retry }) {
  el('register-sub').textContent = sub;
  el('register-spinner').hidden = !spinner;
  el('register-qr').hidden = !qrLink;
  if (qrLink) el('register-qr').src = qrUrl(qrLink);
  el('register-link-btn').hidden = !qrLink;
  if (qrLink) el('register-link-btn').onclick = () => openExternal(qrLink);
  el('register-retry-btn').hidden = !retry;
}

async function startSignin() {
  clearTimeout(signinPollTimer);
  showPanel('register-panel');
  renderSignin({ sub: 'Setting up your Xaman sign-in…', spinner: true });
  try {
    const s = await api('/api/signin', { method: 'POST', body: JSON.stringify(discordCtx()) });
    renderSignin({
      sub: 'Scan with Xaman and approve the sign-in — your wallet address is captured automatically.',
      qrLink: s.signin_link,
    });
    pollSignin(s.uuid);
  } catch (e) {
    showError(e.message);
    renderSignin({ sub: 'Could not start the Xaman sign-in.', retry: true });
  }
}

function pollSignin(uuid) {
  clearTimeout(signinPollTimer);
  const tick = async () => {
    if (el('register-panel').hidden) return; // user navigated away
    let s;
    try {
      s = await api(`/api/signin/${uuid}`);
    } catch (e) {
      signinPollTimer = setTimeout(tick, 3000); // transient; keep polling
      return;
    }
    if (s.state === 'signed') {
      me.wallet = s.wallet;
      showMintHome();
      return;
    }
    if (s.state === 'expired') {
      renderSignin({ sub: 'The sign-in request expired.', retry: true });
      return;
    }
    if (s.state === 'opened') {
      renderSignin({ sub: 'QR scanned — approve the sign-in in Xaman…', spinner: true });
    }
    signinPollTimer = setTimeout(tick, 3000);
  };
  signinPollTimer = setTimeout(tick, 3000);
}

// --- Trait Swapper ---

let swapNfts = [];
let swapPick = [];
let swapCards = []; // {nft, card} for every grid tile, for re-rendering picks
let swapPollTimer = null;
let swappableTraits = [];
let swapFee = null; // {pay_with, amount, per_nft} quote from /api/nfts
let swapMatrix = null; // {universal_layers, pairs} quote from /api/nfts

// Mirrors trait_config.TraitConfig.swap_allowed() (lfg_core/trait_config.py)
// so the trait checklist can be filtered client-side to what the server
// will actually accept for the selected pair's bodies (#30 Task 15). The
// server re-enforces this in handle_swap_start — this is UI-only.
function swapAllowed(matrix, bodyA, bodyB, layer) {
  if (bodyA === bodyB || matrix.universal_layers.includes(layer)) return true;
  return matrix.pairs.some((p) => {
    if (!p.bodies.includes(bodyA) || !p.bodies.includes(bodyB)) return false;
    if (p.layers) return p.layers.includes(layer);
    return !p.layers_except.includes(layer);
  });
}

function showGridSkeletons(grid, count = 6) {
  grid.replaceChildren(...Array.from({ length: count }, () => {
    const card = document.createElement('div');
    card.className = 'nft-card skeleton';
    card.setAttribute('aria-hidden', 'true');
    const img = document.createElement('div');
    img.className = 'ph-img';
    const line = document.createElement('div');
    line.className = 'ph-line';
    card.replaceChildren(img, line);
    return card;
  }));
}

async function openSwapper() {
  showPanel('swap-panel');
  swapPick = [];
  swapCards = [];
  el('pick-traits-btn').disabled = true;
  showGridSkeletons(el('nft-grid'));
  status('Loading your GOs…');
  try {
    const data = await api('/api/nfts');
    swapNfts = data.nfts;
    swappableTraits = data.swappable_traits || [];
    swapFee = data.swap_fee || null;
    swapMatrix = data.swap_matrix || null;
    status('');
    el('nft-grid').replaceChildren(); // drop the skeleton loaders
    if (!swapNfts.length) {
      el('swap-help').textContent = 'No swappable GOs here yet. Time to build.';
      return;
    }
    for (const nft of swapNfts) {
      const card = document.createElement('button');
      card.className = 'nft-card';
      // NFT metadata is untrusted — build DOM nodes, never innerHTML.
      const pick = document.createElement('span');
      pick.className = 'pick';
      pick.setAttribute('aria-hidden', 'true');
      const img = document.createElement('img');
      img.src = imgUrl(nft.image);
      img.alt = '';
      const name = document.createElement('span');
      name.className = 'cap';
      name.textContent = nft.name;
      const body = document.createElement('span');
      body.className = 'body';
      body.textContent = nft.gender; // male / female / skeleton / ape
      name.appendChild(body);
      card.replaceChildren(pick, img, name);
      card.onclick = () => toggleNftPick(nft, card);
      el('nft-grid').appendChild(card);
      swapCards.push({ nft, card });
    }
    renderPicks();
  } catch (e) {
    el('nft-grid').replaceChildren(); // drop the skeleton loaders
    showError(e.message);
  }
}

function toggleNftPick(nft, card) {
  const idx = swapPick.findIndex((p) => p.nft.nft_id === nft.nft_id);
  if (idx >= 0) swapPick.splice(idx, 1);
  else if (swapPick.length < 2) swapPick.push({ nft, card });
  else return;
  renderPicks();
}

// Cross-body pairs are allowed now (#30) — picking no longer locks to a
// matching body type. Which traits are offered for the selected pair is
// decided later, per layer, in showTraitChooser() via swapAllowed().
function renderPicks() {
  for (const { nft, card } of swapCards) {
    card.classList.remove('sel-1', 'sel-2');
    card.disabled = false;
    const badge = card.querySelector('.pick');
    badge.textContent = '';
    const i = swapPick.findIndex((p) => p.nft.nft_id === nft.nft_id);
    if (i >= 0) {
      card.classList.add(`sel-${i + 1}`);
      badge.textContent = String(i + 1);
    }
  }
  el('pick-traits-btn').disabled = swapPick.length !== 2;
  el('swap-help').textContent = swapPick.length === 0
    ? 'Pick your first avatar.'
    : swapPick.length === 1
      ? 'Now pick a second avatar to swap with.'
      : 'Pair locked in — pick the traits to swap.';
}

function traitValue(nft, traitType) {
  const a = nft.attributes.find((t) => t.trait_type === traitType);
  return a ? a.value : 'None';
}

// Category color rotation for the trait-row dots (brand kit series palette).
const TRAIT_DOT_COLORS = ['#4890C0', '#601878', '#D84830', '#D89030',
                          '#F0D848', '#3DA35D', '#7FB3D8', '#B07A3A'];

// Cost line above the final CTA. Same silent-path pattern as the mint: BRIX
// holders see BRIX, everyone else the XRP price — no trustline talk.
function renderSwapCost() {
  const cost = el('swap-cost');
  if (!swapFee) { cost.hidden = true; return; }
  cost.hidden = false;
  if (swapFee.pay_with === 'XRP') {
    const xrp = Number(swapFee.amount);
    cost.textContent = `Swap cost: ~${Number.isFinite(xrp) ? xrp.toFixed(2) : swapFee.amount} XRP`;
  } else {
    cost.textContent = `Swap cost: ${swapFee.amount} BRIX — ${swapFee.per_nft} per avatar.`;
  }
}

function showTraitChooser() {
  if (swapPick.length !== 2) return;
  const [a, b] = swapPick.map((p) => p.nft);
  showPanel('swap-traits-panel');
  renderSwapCost();
  el('swap-img1').src = imgUrl(a.image);
  el('swap-img2').src = imgUrl(b.image);
  el('swap-name1').textContent = a.name;
  el('swap-name2').textContent = b.name;
  const list = el('trait-list');
  list.innerHTML = '';
  // Only offer traits the server's swap matrix actually permits for this
  // pair's bodies (#30 Task 15) — swap_allowed() on the server is still the
  // real gate; this just keeps the checklist from showing dead ends.
  const offeredTraits = swapMatrix
    ? swappableTraits.filter((trait) => swapAllowed(swapMatrix, a.gender, b.gender, trait))
    : swappableTraits;
  for (const [i, trait] of offeredTraits.entries()) {
    const row = document.createElement('label');
    row.className = 'trait-row';
    row.style.setProperty('--cat', TRAIT_DOT_COLORS[i % TRAIT_DOT_COLORS.length]);
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = trait;
    const label = document.createElement('strong');
    label.textContent = trait;
    const values = document.createElement('span');
    values.textContent = `${traitValue(a, trait)} ↔ ${traitValue(b, trait)}`;
    row.replaceChildren(input, label, values);
    list.appendChild(row);
  }
}

const SWAP_STAGE_TEXT = {
  composing: ['🎨 Crafting new builds', 'Composing the swapped images…'],
  uploading: ['☁️ Uploading', 'Saving the new images and metadata to the CDN…'],
  burning: ['🔥 Burning originals', 'Burning the original NFTs on XRPL…'],
  minting: ['⛏️ Reminting', 'Minting the re-crafted NFTs…'],
  modifying: ['🔄 Updating on-chain', 'Updating your mutable NFTs in place via NFTokenModify…'],
  creating_offers: ['📨 Creating offers', 'Preparing the offers back to your wallet…'],
};

async function confirmSwap() {
  const traits = [...el('trait-list').querySelectorAll('input:checked')]
    .map((i) => i.value);
  if (!traits.length) { status('Select at least one trait to swap.'); return; }
  const [a, b] = swapPick.map((p) => p.nft);
  try {
    const s = await api('/api/swap', {
      method: 'POST',
      body: JSON.stringify({ nft1_id: a.nft_id, nft2_id: b.nft_id, traits, ...discordCtx() }),
    });
    showPanel('swap-result-panel');
    el('swap-results').innerHTML = '';
    el('swap-done-btn').hidden = true;
    pollSwap(s.id);
  } catch (e) {
    showError(e.message);
  }
}

function renderSwapProgress(state) {
  const [title, text] = SWAP_STAGE_TEXT[state] || ['Working…', ''];
  el('swap-result-title').textContent = title;
  el('swap-result-text').textContent = text;
  el('swap-results').replaceChildren();
}

// In-place (NFTokenModify) swaps are paid upfront: show the BRIX payment QR.
let swapPaymentShown = null; // session id the QR is rendered for
function renderSwapPayment(s) {
  if (swapPaymentShown === s.id) return; // already on screen; don't rebuild
  swapPaymentShown = s.id;
  el('swap-result-title').textContent = '💰 Swap fee required';
  el('swap-result-text').textContent =
    `Pay ${s.fee_amount} ${s.pay_with || 'BRIX'} to swap your NFT(s) in place. ` +
    'Scan the QR with Xaman/XUMM or open the link, approve, then wait here.';
  const box = el('swap-results');
  const qrImg = document.createElement('img');
  qrImg.className = 'result-qr';
  qrImg.src = qrUrl(s.payment_link);
  qrImg.alt = 'QR';
  const btn = document.createElement('button');
  btn.className = 'link';
  btn.textContent = 'Open in Xaman';
  btn.onclick = () => openExternal(s.payment_link);
  box.replaceChildren(qrImg, btn);
}

function renderSwapResults(s) {
  const needsAccept = s.results.some((r) => !r.modified);
  el('swap-result-title').textContent = '🎉 Traits swapped!';
  el('swap-result-text').textContent = needsAccept
    ? 'Scan each QR (or open in Xaman) to accept your re-crafted NFTs.'
    : 'Your NFTs were updated in place — the new traits are already in your wallet.';
  const box = el('swap-results');
  box.innerHTML = '';
  for (const r of s.results) {
    const div = document.createElement('div');
    div.className = 'swap-result';
    const h3 = document.createElement('h3');
    h3.textContent = r.name;
    const resultImg = document.createElement('img');
    resultImg.className = 'result-img';
    resultImg.src = imgUrl(r.image_url);
    resultImg.alt = '';
    div.replaceChildren(h3, resultImg);
    if (r.modified) {
      // Updated via NFTokenModify — nothing to accept.
      const note = document.createElement('span');
      note.className = 'modified-note';
      note.textContent = '✅ Updated in your wallet — no action needed.';
      div.appendChild(note);
    } else {
      const qrImg = document.createElement('img');
      qrImg.className = 'result-qr';
      qrImg.src = qrUrl(r.accept_deeplink);
      qrImg.alt = 'QR';
      div.appendChild(qrImg);
      const btn = document.createElement('button');
      btn.className = 'link';
      btn.textContent = 'Open in Xaman';
      btn.onclick = () => openExternal(r.accept_deeplink);
      div.appendChild(btn);
    }
    box.appendChild(div);
  }
  el('swap-done-btn').hidden = false;
}

function pollSwap(sessionId) {
  clearTimeout(swapPollTimer);
  const tick = async () => {
    let s;
    try {
      s = await api(`/api/swap/${sessionId}`);
    } catch (e) {
      swapPollTimer = setTimeout(tick, 3000); // transient; keep polling
      return;
    }
    if (s.state === 'offers_ready') {
      renderSwapResults(s);
      return;
    }
    if (s.state === 'payment_timeout') {
      el('swap-result-title').textContent = '⏰ Payment timed out';
      el('swap-result-text').textContent =
        s.error || 'No swap fee was received in time. Your NFTs are untouched.';
      el('swap-results').replaceChildren();
      el('swap-done-btn').hidden = false;
      return;
    }
    if (s.state === 'awaiting_payment') {
      if (s.payment_link) renderSwapPayment(s);
      swapPollTimer = setTimeout(tick, 3000);
      return;
    }
    if (s.state === 'failed') {
      // A partial failure can still carry accept offers the user MUST claim
      // (their original was burned) — render them alongside the error.
      if (s.results && s.results.length) renderSwapResults(s);
      el('swap-result-title').textContent =
        s.results && s.results.length ? '⚠️ Swap partially failed' : '❌ Swap failed';
      el('swap-result-text').textContent = s.error || 'Something went wrong.';
      el('swap-done-btn').hidden = false;
      return;
    }
    if (SWAP_STAGE_TEXT[s.state]) renderSwapProgress(s.state);
    swapPollTimer = setTimeout(tick, 3000);
  };
  swapPollTimer = setTimeout(tick, 3000);
}

// --- Dressing Room ---
let economyState = null;
let activeNftId = null;

function layerSrc(body, trait, value) {
  return `/api/layer?body=${encodeURIComponent(body)}` +
         `&trait=${encodeURIComponent(trait)}&value=${encodeURIComponent(value)}`;
}

// A layer request only renders when both body and value are present and the
// value isn't the literal "None". Freshly-minted / not-yet-indexed tokens have
// an empty body and/or missing attributes; issuing a layer fetch for those 400s
// (empty params), so callers must guard with this before building a layerSrc.
function layerComplete(body, value) {
  return Boolean(body) && Boolean(value) && value !== 'None';
}

// 1x1 transparent PNG — a non-broken placeholder for incomplete NFTs.
const BLANK_IMG =
  'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';

function renderCanvas(char) {
  const canvas = el('dressup-canvas');
  canvas.replaceChildren();
  const order = economyState.trait_order;
  // Incomplete metadata (empty body) means every layer fetch would 400; show a
  // graceful "still indexing" state instead of a wall of broken images.
  if (!char.body) {
    canvas.classList.add('incomplete');
    el('dressup-id').textContent = `#${char.edition} · still indexing…`;
    return;
  }
  canvas.classList.remove('incomplete');
  const byType = Object.fromEntries(char.attributes.map((a) => [a.trait_type, a.value]));
  for (const slot of order) {
    const value = byType[slot];
    if (!layerComplete(char.body, value)) continue;
    const img = document.createElement('img');
    img.src = layerSrc(char.body, slot, value);
    img.alt = '';
    canvas.appendChild(img);
  }
  el('dressup-id').textContent = `#${char.edition} · ${char.body} · live`;
}

function renderRoster(assembleEnabled = true) {
  const strip = el('roster-strip');
  strip.replaceChildren();
  for (const char of economyState.characters) {
    const tile = document.createElement('button');
    tile.className = 'roster-tile' + (char.nft_id === activeNftId ? ' active' : '');
    const img = document.createElement('img');
    const cdn = imgUrl(char.image_url);
    const bodyVal = (char.attributes.find((a) => a.trait_type === 'Body') || {}).value;
    if (cdn) {
      img.src = cdn;
    } else if (layerComplete(char.body, bodyVal)) {
      img.src = layerSrc(char.body, 'Body', bodyVal);
    } else {
      // No CDN image and incomplete metadata: a layer fetch would 400. Render a
      // placeholder tile (transparent img) rather than a broken one.
      tile.classList.add('incomplete');
      img.src = BLANK_IMG;
    }
    img.alt = `#${char.edition}`;
    tile.appendChild(img);
    tile.onclick = () => selectCharacter(char.nft_id);
    strip.appendChild(tile);
  }
  const add = document.createElement('button');
  add.className = 'roster-tile assemble';
  add.textContent = '＋';
  add.title = 'Assemble new';
  if (assembleEnabled) {
    add.onclick = () => openAssemble();
  } else {
    add.disabled = true;
    add.title = 'Create your Closet first';
  }
  strip.appendChild(add);
}

function selectCharacter(nftId) {
  activeNftId = nftId;
  const char = economyState.characters.find((c) => c.nft_id === nftId);
  if (char) renderCanvas(char);
  renderRoster();
  renderCloset();
}

// Returns the Closet issuance status from the nested token path.
// economyState.closet.token.status is the authoritative key (not .closet.status).
function closetStatus() {
  return (economyState.closet && economyState.closet.token && economyState.closet.token.status) || 'none';
}

async function openDressup() {
  showPanel('dressup-panel');
  status('Loading your wardrobe…');
  try {
    economyState = await api('/api/economy');
    status('');

    const cStatus = closetStatus();
    const gate = el('closet-gate');
    const gateMsg = el('closet-gate-msg');
    const gateBtn = el('closet-gate-btn');
    const harvestBtn = el('dressup-harvest-btn');

    if (cStatus !== 'active') {
      // Show gate; hide/disable Harvest. Reset the gate button: it gets disabled
      // while a POST /api/closet is in flight, and the same persistent DOM node
      // is reused when we re-render the gate (e.g. still pending_accept).
      gate.hidden = false;
      gateBtn.disabled = false;
      harvestBtn.disabled = true;
      harvestBtn.hidden = true;

      if (cStatus === 'none') {
        gateMsg.textContent = 'You need a Closet to store your traits.';
        gateBtn.textContent = 'Create your Closet';
        gateBtn.onclick = async () => {
          gateBtn.disabled = true;
          status('Creating your Closet…');
          try {
            const r = await api('/api/closet', { method: 'POST' });
            if (r.accept) {
              showFlow({ title: '👜 Create your Closet',
                text: 'Scan to accept your Closet in Xaman.',
                qrData: r.accept, link: r.accept, done: true });
            }
            economyState = await api('/api/economy');
            openDressup();
          } catch (e) {
            showError(e.message);
            gateBtn.disabled = false;
            status('');
          }
        };
      } else {
        // pending_accept
        gateMsg.textContent = 'Your Closet is waiting — accept it in Xaman to continue.';
        gateBtn.textContent = 'Finish claiming your Closet';
        gateBtn.onclick = async () => {
          gateBtn.disabled = true;
          status('Fetching your Closet QR…');
          try {
            const r = await api('/api/closet', { method: 'POST' });
            if (r.accept) {
              showFlow({ title: '👜 Finish claiming your Closet',
                text: 'Scan to accept your Closet in Xaman.',
                qrData: r.accept, link: r.accept, done: true });
            }
            economyState = await api('/api/economy');
            openDressup();
          } catch (e) {
            showError(e.message);
            gateBtn.disabled = false;
            status('');
          }
        };
      }

      // Render roster (no-op visually) but don't wire assemble tile
      renderRoster(/* assembleEnabled= */ false);
      el('dressup-canvas').replaceChildren();
      return;
    }

    // Closet active — full Dressing Room
    gate.hidden = true;
    harvestBtn.disabled = false;
    harvestBtn.hidden = false;
    harvestBtn.onclick = () => harvestActive();

    activeNftId = economyState.characters[0] ? economyState.characters[0].nft_id : null;
    renderRoster(/* assembleEnabled= */ true);
    if (activeNftId) selectCharacter(activeNftId);
    else { el('dressup-canvas').replaceChildren(); renderCloset(); }
  } catch (e) {
    showError(e.message);
  }
}

let closetFilter = 'All';
let equipBusy = false;
let extractBusy = {};   // keyed by `${slot}:${value}` to guard per-tile double-clicks
let depositBusy = {};   // keyed by nft_id

function activeChar() {
  return economyState.characters.find((c) => c.nft_id === activeNftId) || null;
}

function renderClosetFilter() {
  const sel = el('closet-filter');
  const slots = ['All', ...economyState.slots];
  sel.replaceChildren();
  for (const s of slots) {
    const o = document.createElement('option');
    o.value = s; o.textContent = s; sel.appendChild(o);
  }
  sel.value = closetFilter;
  sel.onchange = () => { closetFilter = sel.value; renderCloset(); };
}

function renderCloset() {
  renderClosetFilter();
  const grid = el('closet-grid');
  grid.replaceChildren();
  const char = activeChar();
  for (const asset of economyState.closet.assets) {
    if (closetFilter !== 'All' && asset.slot !== closetFilter) continue;
    // The tile is a non-button container (not a <button>) so the Extract control
    // can be a valid nested <button> AND remain usable even when equip is not
    // available — extraction does not depend on equip compatibility.
    const item = document.createElement('div');
    item.className = 'closet-item';
    item.setAttribute('role', 'button');
    item.tabIndex = 0;
    // Compatibility: only allow equip when this asset can go on the active character.
    // Client mirrors the server precheck (server re-verifies on commit).
    const compatible = char && economyState.slots.includes(asset.slot);
    if (!compatible) item.classList.add('incompatible');
    const img = document.createElement('img');
    // Guard: a missing active body or empty asset value would 400 the layer fetch.
    img.src = (char && layerComplete(char.body, asset.value))
      ? layerSrc(char.body, asset.slot, asset.value)
      : BLANK_IMG;
    img.alt = `${asset.slot}: ${asset.value}`;
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = `×${asset.count}`;
    // Extract button: pull this loose trait out as a tradeable NFToken.
    const extractBtn = document.createElement('button');
    extractBtn.className = 'extract';
    extractBtn.title = 'Extract as tradeable trait';
    extractBtn.textContent = '↑';
    extractBtn.onclick = (e) => {
      e.stopPropagation();  // don't also fire the tile equip click
      extractTrait(asset.slot, asset.value, extractBtn);
    };
    item.replaceChildren(img, count, extractBtn);
    // Equip is wired only when the asset is compatible with the active character;
    // the tile still renders (and Extract still works) when it isn't.
    if (compatible) item.onclick = () => equipTrait(asset.slot, asset.value, item);
    grid.appendChild(item);
  }
  renderTraitStrip();
}

function renderTraitStrip() {
  const strip = el('trait-strip');
  if (!strip) return;
  strip.replaceChildren();
  const tokens = (economyState.trait_tokens) || [];
  if (!tokens.length) {
    const hint = document.createElement('p');
    hint.className = 'trait-strip-empty';
    hint.textContent = 'No extracted traits';
    strip.appendChild(hint);
    return;
  }
  const char = activeChar();
  for (const t of tokens) {
    const chip = document.createElement('div');
    chip.className = 'trait-chip';
    const img = document.createElement('img');
    img.src = (char && layerComplete(char.body, t.value))
      ? layerSrc(char.body, t.slot, t.value)
      : BLANK_IMG;
    img.alt = `${t.slot}: ${t.value}`;
    const label = document.createElement('span');
    label.className = 'trait-chip-label';
    label.textContent = `${t.slot}: ${t.value}`;
    const depositBtn = document.createElement('button');
    depositBtn.className = 'deposit';
    depositBtn.textContent = 'Deposit';
    depositBtn.onclick = () => depositTrait(t.nft_id, depositBtn);
    chip.replaceChildren(img, label, depositBtn);
    strip.appendChild(chip);
  }
}

async function extractTrait(slot, value, btnEl) {
  if (closetStatus() !== 'active') return;
  const key = `${slot}:${value}`;
  if (extractBusy[key]) return;
  extractBusy[key] = true;
  btnEl.disabled = true;
  status('Extracting trait…');
  try {
    const res = await api('/api/extract', {
      method: 'POST',
      body: JSON.stringify({ slot, value }),
    });
    const final = await pollEconomyOp('extract', res);
    status('');
    if (final.state === 'failed') throw new Error(final.error || 'extract failed');
    if (final.accept) {
      showFlow({
        title: '🎟️ Extract trait',
        text: 'Scan to accept your tradeable trait in Xaman.',
        qrData: final.accept,
        link: final.accept,
        done: true,
      });
    }
    economyState = await api('/api/economy');
    renderCloset();
  } catch (e) {
    showError(e.message);
    status('');
  } finally {
    extractBusy[key] = false;
    btnEl.disabled = false;
  }
}

async function depositTrait(nftId, btnEl) {
  if (closetStatus() !== 'active') return;
  if (depositBusy[nftId]) return;
  depositBusy[nftId] = true;
  btnEl.disabled = true;
  status('Depositing trait…');
  try {
    const res = await api('/api/deposit', {
      method: 'POST',
      body: JSON.stringify({ nft_id: nftId }),
    });
    const final = await pollEconomyOp('deposit', res);
    status('');
    if (final.state === 'failed') throw new Error(final.error || 'deposit failed');
    economyState = await api('/api/economy');
    renderCloset();
  } catch (e) {
    showError(e.message);
    status('');
  } finally {
    depositBusy[nftId] = false;
    btnEl.disabled = false;
  }
}

async function equipTrait(slot, value, tileEl) {
  if (equipBusy || !activeChar()) return;       // in-flight lock
  equipBusy = true;
  tileEl.classList.add('busy');
  // Optimistic client stack: update the active character's attribute now.
  const char = activeChar();
  const attr = char.attributes.find((a) => a.trait_type === slot);
  const previous = attr ? attr.value : 'None';
  if (attr) attr.value = value;
  renderCanvas(char);
  try {
    const res = await api('/api/equip', {
      method: 'POST',
      body: JSON.stringify({ nft_id: activeNftId, slot, value }),
    });
    const final = await pollEconomyOp('equip', res);
    if (final.state === 'failed') throw new Error(final.error || 'equip failed');
    // Reconcile the Closet from authoritative state.
    economyState = await api('/api/economy');
    selectCharacter(activeNftId);
  } catch (e) {
    if (attr) attr.value = previous;             // revert optimistic stack
    renderCanvas(char);
    showError(e.message);
  } finally {
    equipBusy = false;
    tileEl.classList.remove('busy');
  }
}

function isTerminal(s) { return s === 'done' || s === 'failed'; }

function pollEconomyOp(kind, startResp) {
  if (isTerminal(startResp.state)) return Promise.resolve(startResp);
  const id = startResp.id;
  const MAX_ATTEMPTS = 100; // ~5 min at 3 s/tick
  let attempts = 0;
  return new Promise((resolve) => {
    const tick = async () => {
      attempts++;
      if (attempts > MAX_ATTEMPTS) {
        resolve({ state: 'failed', error: 'timed out — please refresh and try again' });
        return;
      }
      let s;
      try {
        s = await api(`/api/${kind}/${id}`);
      } catch (e) {
        setTimeout(tick, 3000); // transient; keep polling
        return;
      }
      if (isTerminal(s.state)) resolve(s);
      else setTimeout(tick, 3000);
    };
    setTimeout(tick, 3000);
  });
}

async function harvestActive() {
  const char = activeChar();
  if (!char) return;
  if (!(await confirmDialog({
    title: 'Harvest this character?',
    text: `This permanently burns #${char.edition}. Its parts go to your Closet.`,
    confirmLabel: '🔥 Harvest',
  }))) return;
  status('Harvesting…');
  try {
    const res = await api('/api/harvest', {
      method: 'POST', body: JSON.stringify({ nft_id: char.nft_id }),
    });
    const final = await pollEconomyOp('harvest', res);
    status('');
    if (final.state === 'failed') throw new Error(final.error || 'harvest failed');
    economyState = await api('/api/economy');
    activeNftId = economyState.characters[0] ? economyState.characters[0].nft_id : null;
    showPanel('dressup-panel');
    if (activeNftId) selectCharacter(activeNftId);
    else { renderRoster(); renderCloset(); el('dressup-canvas').replaceChildren(); }
  } catch (e) {
    showError(e.message);
  }
}

async function openAssemble() {
  const bodies = economyState.closet.bodies;
  if (!bodies.length) { showError('No bodies in your Closet to assemble.'); return; }
  // MVP: assemble the first available body edition, auto-filling each slot with the
  // first compatible Closet asset; the user reviews the preview before committing.
  const edition = bodies[0];
  const chosen = {};
  for (const slot of economyState.slots) {
    const asset = economyState.closet.assets.find((a) => a.slot === slot && a.count > 0);
    if (asset) chosen[slot] = asset.value;
  }
  const missing = economyState.slots.filter((s) => !(s in chosen));
  if (missing.length) {
    showError(`Closet is missing assets for: ${missing.join(', ')}`);
    return;
  }
  if (!(await confirmDialog({
    title: 'Assemble new character?',
    text: `Assemble a new character for edition #${edition}?`,
    confirmLabel: 'Assemble',
  }))) return;
  commitAssemble(edition, chosen);
}

async function commitAssemble(edition, chosen) {
  status('Assembling…');
  try {
    const res = await api('/api/assemble', {
      method: 'POST', body: JSON.stringify({ edition, chosen }),
    });
    const final = await pollEconomyOp('assemble', res);
    status('');
    if (final.state === 'failed') throw new Error(final.error || 'assemble failed');
    showFlow({ title: `🎉 #${edition} assembled!`,
      text: final.accept ? 'Scan to accept your new character in Xaman.'
                         : 'Your new character is on its way.',
      qrData: final.accept || null, link: final.accept || null,
      image: imgUrl(final.image_url), done: true, celebrate: true });
    economyState = await api('/api/economy');
  } catch (e) {
    showError(e.message);
  }
}

// --- Marketplace (#44 Task 10) ---
//
// IA: one market-panel with Browse (Characters|Traits kind toggle, trait/
// price filters, price-sorted sticker-card grid) and Mine (my listings with
// Cancel; unlisted characters + wallet trait tokens with List; loose Closet
// traits with Sell -> the two-step wizard) — spec §Q8. Every action (list,
// cancel, buy, the trait-sell wizard) is driven by the single marketFlow()
// start->QR->poll helper below, reusing flow-panel/showFlow exactly like
// the mint/swap/economy flows (no new QR machinery).

const MARKET_STATUS_PATH = {
  list: (id) => `/api/market/list/${id}`,
  cancel: (id) => `/api/market/cancel/${id}`,
  buy: (id) => `/api/market/buy/${id}`,
  trait_list: (id) => `/api/market/trait/list/${id}`,
};

const marketState = { tab: 'browse', kind: 'character' };
let marketPendingItem = null; // the character/trait/closet-asset the list-form panel is acting on
let marketFlowTimer = null;

function highlightTabs(containerId, dataKey, activeValue) {
  for (const btn of el(containerId).querySelectorAll('.lb-chip')) {
    const active = btn.dataset[dataKey] === activeValue;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', String(active));
  }
}

async function openMarket() {
  showPanel('market-panel');
  marketState.tab = 'browse';
  el('market-browse').hidden = false;
  el('market-mine').hidden = true;
  highlightTabs('market-tabs', 'tab', 'browse');
  highlightTabs('market-kind', 'kind', marketState.kind);
  await loadMarketBrowse();
}

function switchMarketTab(tab) {
  marketState.tab = tab;
  highlightTabs('market-tabs', 'tab', tab);
  el('market-browse').hidden = tab !== 'browse';
  el('market-mine').hidden = tab !== 'mine';
  if (tab === 'browse') loadMarketBrowse();
  else loadMarketMine();
}

// A trait-image URL from the backend (/api/layer?...) is already same-origin
// and must NOT go through the CDN proxy (imgUrl); a character's `image` is an
// absolute CDN URL and must. Mirrors the same distinction renderCanvas/
// renderCloset draw between layerSrc() and imgUrl() elsewhere in this file.
function marketRowImgSrc(vm) {
  if (!vm.image) return null;
  return vm.kind === 'trait' ? vm.image : imgUrl(vm.image);
}

function renderMarketGrid(rows) {
  const grid = el('market-grid');
  const empty = el('market-empty');
  grid.replaceChildren();
  if (!rows.length) { empty.hidden = false; return; }
  empty.hidden = true;
  for (const row of rows) {
    const vm = marketPure.mapListingRow(row);
    const card = document.createElement('button');
    card.className = 'nft-card';
    const img = document.createElement('img');
    img.src = marketRowImgSrc(vm) || BLANK_IMG;
    img.alt = '';
    const name = document.createElement('span');
    name.className = 'cap';
    name.textContent = vm.title;
    const price = document.createElement('span');
    price.className = 'market-card-price';
    price.textContent = `${vm.amountXrp} XRP`;
    name.appendChild(price);
    card.replaceChildren(img, name);
    card.onclick = () => openBuyFlow(row);
    grid.appendChild(card);
  }
}

async function loadMarketBrowse() {
  highlightTabs('market-kind', 'kind', marketState.kind);
  const grid = el('market-grid');
  showGridSkeletons(grid);
  el('market-empty').hidden = true;
  const slot = el('market-trait-slot').value.trim();
  const value = el('market-trait-value').value.trim();
  const traits = slot && value ? [marketPure.traitFilterToken(slot, value)] : [];
  const pairs = marketPure.buildListingsParams({
    kind: marketState.kind,
    traits,
    minXrp: el('market-min-xrp').value.trim(),
    maxXrp: el('market-max-xrp').value.trim(),
    sort: el('market-sort').value,
    limit: 24,
    offset: 0,
  });
  const qs = new URLSearchParams();
  for (const [k, v] of pairs) qs.append(k, v);
  try {
    const data = await api(`/api/market/listings?${qs.toString()}`);
    renderMarketGrid(data.rows || []);
  } catch (e) {
    grid.replaceChildren();
    showError(e.message);
  }
}

// Populate the trait-slot filter <select> from the swap matrix's slot list
// (the same swappable-traits data the Trait Swapper already fetches via
// /api/nfts) so it reads "trait selects" rather than free text, without a
// second wallet-specific economy fetch.
async function ensureMarketTraitSlotOptions() {
  const sel = el('market-trait-slot');
  if (sel.options.length > 1) return; // already populated this session
  try {
    const data = await api('/api/nfts');
    for (const slot of data.swappable_traits || []) {
      const o = document.createElement('option');
      o.value = slot; o.textContent = slot;
      sel.appendChild(o);
    }
  } catch (e) { /* filter still works with free-text value matching */ }
}

function renderChipList(containerEl, emptyEl, entries, actionLabel, onAction) {
  containerEl.replaceChildren();
  if (!entries.length) { emptyEl.hidden = false; return; }
  emptyEl.hidden = true;
  for (const entry of entries) {
    const chip = document.createElement('div');
    chip.className = 'trait-chip';
    const img = document.createElement('img');
    img.src = entry.imgSrc || BLANK_IMG;
    img.alt = '';
    const label = document.createElement('span');
    label.className = 'trait-chip-label';
    label.textContent = entry.label;
    const btn = document.createElement('button');
    btn.className = 'chip-action';
    btn.textContent = actionLabel;
    btn.onclick = () => onAction(entry.payload);
    chip.replaceChildren(img, label, btn);
    containerEl.appendChild(chip);
  }
}

// Best-effort trait art for Mine's unlisted-traits/loose-Closet chips: reuses
// the active Dressing Room character's body (if the economy state happens to
// be loaded already) exactly like renderTraitStrip() does; falls back to no
// image rather than fetching economy state just for a thumbnail.
function mineTraitImgSrc(slot, value) {
  if (!economyState) return null;
  const char = activeChar();
  return char && layerComplete(char.body, value) ? layerSrc(char.body, slot, value) : null;
}

function renderMineGroups(data) {
  const listingEntries = data.listings.map((row) => {
    const vm = marketPure.mapListingRow(row);
    return {
      imgSrc: marketRowImgSrc(vm),
      label: `${vm.title} — ${vm.amountXrp} XRP`,
      payload: row,
    };
  });
  renderChipList(el('mine-listings'), el('mine-listings-empty'), listingEntries, 'Cancel', cancelListing);

  const charEntries = data.unlisted_characters.map((c) => {
    const label = c.nft_number != null ? `#${c.nft_number}` : c.nft_id;
    return {
      imgSrc: c.image ? imgUrl(c.image) : null,
      label,
      payload: { nftId: c.nft_id, label, wizard: false },
    };
  });
  renderChipList(el('mine-characters'), el('mine-characters-empty'), charEntries, 'List', openListForm);

  const traitEntries = data.unlisted_trait_tokens.map((t) => ({
    imgSrc: mineTraitImgSrc(t.slot, t.value),
    label: `${t.slot}: ${t.value}`,
    payload: { nftId: t.nft_id, slot: t.slot, value: t.value, label: `${t.slot}: ${t.value}`, wizard: false },
  }));
  renderChipList(el('mine-traits'), el('mine-traits-empty'), traitEntries, 'List', openListForm);

  const closetEntries = data.closet_assets.map((a) => ({
    imgSrc: mineTraitImgSrc(a.slot, a.value),
    label: `${a.slot}: ${a.value} ×${a.count}`,
    payload: { slot: a.slot, value: a.value, label: `${a.slot}: ${a.value}`, wizard: true },
  }));
  renderChipList(el('mine-closet'), el('mine-closet-empty'), closetEntries, 'Sell', openListForm);
}

async function loadMarketMine() {
  try {
    const data = await api('/api/market/mine');
    renderMineGroups(data);
  } catch (e) {
    showError(e.message);
  }
}

// --- marketFlow: the single start -> QR -> poll driver (spec §Q8), reused
// by list/cancel/buy/trait-sell. `render(sessionDict)` maps that op's
// session shape to a showFlow() view; marketFlow itself knows nothing
// op-specific beyond routing to the right status endpoint by `kind`. ---

async function promptClosetRequired() {
  const go = await confirmDialog({
    title: 'Closet required',
    text: marketPure.CLOSET_REQUIRED_MESSAGE,
    confirmLabel: 'Go to Closet',
  });
  if (go) openDressup();
}

function pollMarketFlow(kind, sessionId, render) {
  clearTimeout(marketFlowTimer);
  const path = MARKET_STATUS_PATH[kind](sessionId);
  const tick = async () => {
    if (el('flow-panel').hidden) return; // user navigated away
    let s;
    try {
      s = await api(path);
    } catch (e) {
      marketFlowTimer = setTimeout(tick, 3000); // transient; keep polling
      return;
    }
    showFlow(render(s));
    if (!marketPure.isMarketTerminal(s.state)) marketFlowTimer = setTimeout(tick, 3000);
  };
  marketFlowTimer = setTimeout(tick, 3000);
}

async function marketFlow(kind, startPath, body, render) {
  clearTimeout(marketFlowTimer);
  showPanel('flow-panel');
  showFlow({ title: 'Starting…', spinner: true });
  let s;
  try {
    s = await api(startPath, { method: 'POST', body: JSON.stringify(body) });
  } catch (e) {
    if (e.message === 'closet_required') {
      showPanel('market-panel');
      promptClosetRequired();
      return;
    }
    showFlow({ title: '❌ Could not start', text: e.message, done: true });
    return;
  }
  showFlow(render(s));
  if (!marketPure.isMarketTerminal(s.state)) pollMarketFlow(kind, s.id, render);
}

// qrData is the string re-encoded into the branded QR by qrUrl()/`/api/qr.png`.
// It MUST be the Xaman deep link (`xumm_url` = the payload's next.always,
// xumm.app/sign/<uuid>), NOT `qr_url` (XUMM's refs.qr_png IMAGE url): encoding
// an image url into a QR makes a scan open a browser tab showing that image —
// which is itself a QR — instead of opening the sign request in Xaman. Every
// working flow (mint/swap) passes the deep link here; the marketplace must too.
function marketListRender(s) {
  if (s.state === 'pending') {
    return { title: '⏳ Confirming', text: 'Signature received — waiting for the ledger to confirm…', spinner: true };
  }
  if (s.state === 'done') {
    return { title: '🎉 Listed!', text: 'Your listing is live on the Marketplace.', done: true };
  }
  if (s.state === 'awaiting_signature') {
    return { title: '📋 List for sale', text: 'Scan to sign the sell offer in Xaman.', qrData: s.xumm_url, link: s.xumm_url };
  }
  if (s.state === 'unknown') {
    // The finalize poller gave up before confirming, but the listener/backfill
    // self-heal from the ledger — the listing may well have landed.
    return { title: '⏳ Couldn\'t confirm', text: "We couldn't confirm the listing in time — check My Listings shortly; it may still have gone through.", done: true };
  }
  return { title: '❌ Listing failed', text: s.error || 'Something went wrong.', done: true };
}

function marketCancelRender(s) {
  if (s.state === 'done') {
    return { title: '✅ Listing cancelled', text: 'It is no longer for sale.', done: true };
  }
  if (s.state === 'awaiting_signature') {
    return { title: '🗑️ Cancel listing', text: 'Scan to sign the cancellation in Xaman.', qrData: s.xumm_url, link: s.xumm_url };
  }
  return { title: '❌ Cancel failed', text: s.error || 'Something went wrong.', done: true };
}

function marketBuyRender(listingKind) {
  return (s) => {
    if (s.state === 'pending') {
      return { title: '⏳ Confirming', text: 'Signature received — waiting for the ledger to confirm…', spinner: true };
    }
    if (s.state === 'done') {
      return {
        title: '🎉 Purchase complete!',
        text: listingKind === 'trait' ? 'Sold — added to your Closet.' : 'The NFT is on its way to your wallet.',
        done: true,
      };
    }
    if (s.state === 'awaiting_signature') {
      return { title: '💳 Confirm purchase', text: s.instruction || 'Scan to sign the purchase in Xaman.', qrData: s.xumm_url, link: s.xumm_url };
    }
    if (s.reason === 'listing_unavailable') {
      return { title: '⚠️ No longer available', text: 'That listing was just sold or cancelled.', done: true };
    }
    return { title: '❌ Purchase failed', text: s.error || 'Something went wrong.', done: true };
  };
}

function marketTraitListRender(s) {
  const step = marketPure.traitWizardStepLabel(s.state);
  if (s.state === 'extract_pending') {
    return { title: `🎟️ ${step}`, text: 'Preparing your trait token…', spinner: true };
  }
  if (s.state === 'extract_done') {
    return { title: `🎟️ ${step}`, text: 'Scan to accept your trait token in Xaman.', qrData: s.extract_xumm_url, link: s.extract_xumm_url };
  }
  if (s.state === 'list_pending') {
    return { title: `📤 ${step}`, text: 'Scan to sign the sell offer in Xaman.', qrData: s.list_xumm_url, link: s.list_xumm_url };
  }
  if (s.state === 'listed') {
    return { title: '🎉 Listed!', text: 'Your trait is live on the Marketplace.', done: true };
  }
  return { title: '❌ Sell failed', text: s.error || 'Something went wrong.', done: true };
}

async function openBuyFlow(row) {
  const vm = marketPure.mapListingRow(row);
  const royalty = marketPure.computeRoyalty(vm.amountXrp);
  const ok = await confirmDialog({
    title: `Buy ${vm.title}?`,
    text: `${vm.amountXrp} XRP — seller nets ${royalty.receiveXrp} XRP (93% — 7% collection royalty).`,
    confirmLabel: 'Buy now',
  });
  if (!ok) return;
  await marketFlow('buy', '/api/market/buy', { offer_index: row.offer_index }, marketBuyRender(row.kind));
}

async function cancelListing(row) {
  const vm = marketPure.mapListingRow(row);
  const ok = await confirmDialog({
    title: 'Cancel this listing?',
    text: `${vm.title} — ${vm.amountXrp} XRP will no longer be for sale.`,
    confirmLabel: 'Cancel listing',
  });
  if (!ok) return;
  await marketFlow('cancel', '/api/market/cancel', { offer_index: row.offer_index }, marketCancelRender);
}

function openListForm(item) {
  marketPendingItem = item;
  showPanel('market-list-form-panel');
  el('market-list-form-title').textContent = item.wizard ? 'Sell a trait' : 'List for sale';
  el('market-list-form-sub').textContent = item.label;
  el('market-list-price').value = '';
  el('market-list-royalty').hidden = true;
}

function updateListFormRoyaltyPreview() {
  const out = el('market-list-royalty');
  const check = marketPure.validatePrice(el('market-list-price').value.trim());
  if (check.ok) {
    out.hidden = false;
    out.textContent = marketPure.royaltyDisclosure(el('market-list-price').value.trim());
  } else {
    out.hidden = true;
  }
}

async function submitListForm() {
  const item = marketPendingItem;
  if (!item) return;
  const price = el('market-list-price').value.trim();
  const check = marketPure.validatePrice(price);
  if (!check.ok) { showError(check.error); return; }
  const ok = await confirmDialog({
    title: item.wizard ? 'Post this trait for sale?' : 'List for sale?',
    text: marketPure.royaltyDisclosure(price),
    confirmLabel: item.wizard ? 'Post listing' : 'List it',
  });
  if (!ok) return;
  if (item.wizard) {
    await marketFlow(
      'trait_list', '/api/market/trait/list',
      { slot: item.slot, value: item.value, price_xrp: price },
      marketTraitListRender,
    );
  } else {
    await marketFlow('list', '/api/market/list', { nft_id: item.nftId, price_xrp: price }, marketListRender);
  }
}

// Header logo with a text-wordmark fallback. The Activity's CSP forbids
// inline handlers, so the swap is wired here; the load may already have
// failed before this module ran, hence the complete/naturalWidth check.
function setupLogo() {
  const logo = el('logo-img');
  const fallback = () => {
    logo.hidden = true;
    el('wordmark').hidden = false;
    el('wordmark').removeAttribute('aria-hidden');
  };
  logo.addEventListener('error', fallback);
  if (logo.complete && logo.naturalWidth === 0) fallback();
}

async function main() {
  setupLogo();
  setupLeaderboard();
  el('register-retry-btn').onclick = startSignin;
  el('mint-btn').onclick = startMint;
  el('flow-regen-btn').onclick = regeneratePaymentQr;
  el('swap-btn').onclick = () => openDressup();
  el('swapper-btn').onclick = () => openSwapper();
  el('swap-back-btn').onclick = () => showMintHome();
  el('pick-traits-btn').onclick = showTraitChooser;
  el('swap-cancel-btn').onclick = () => openSwapper();
  el('swap-confirm-btn').onclick = confirmSwap;
  el('swap-done-btn').onclick = () => showMintHome();
  el('change-wallet-btn').onclick = () => startSignin();
  el('flow-done-btn').onclick = () => { showMintHome(); };

  // --- Marketplace (#44 Task 10) ---
  el('market-btn').onclick = () => { ensureMarketTraitSlotOptions(); openMarket(); };
  el('market-back-btn').onclick = () => showMintHome();
  el('market-tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.lb-chip');
    if (!btn) return;
    switchMarketTab(btn.dataset.tab);
  });
  el('market-kind').addEventListener('click', (e) => {
    const btn = e.target.closest('.lb-chip');
    if (!btn || btn.dataset.kind === marketState.kind) return;
    marketState.kind = btn.dataset.kind;
    highlightTabs('market-kind', 'kind', marketState.kind);
    loadMarketBrowse();
  });
  el('market-filter-apply').onclick = () => loadMarketBrowse();
  el('market-list-price').addEventListener('input', updateListFormRoyaltyPreview);
  el('market-list-confirm-btn').onclick = submitListForm;
  el('market-list-cancel-btn').onclick = () => showPanel('market-panel');

  // Dev live-reload: runs even in degraded mode (no frame_id).
  try {
    const cfg = await api('/api/config');
    // Closet / trait economy ships after the mainnet MVP: with the feature
    // off, hide the Dress Up entry point (the API answers 403 regardless).
    if (cfg.economy_enabled === false) el('swap-btn').hidden = true;
    if (cfg.dev_mode && 'EventSource' in window) {
      new EventSource('/__dev/reload').onmessage = () => location.reload();
    }
  } catch (_) { /* non-dev or offline: ignore */ }

  if (!insideTelegram && !insideDiscord) {
    status('Open this inside Telegram or Discord. (Dev mode: API calls will be unauthorized.)');
    return;
  }

  try {
    // Same UI either way — only the auth handshake differs by host.
    if (insideTelegram) await setupTelegram();
    else await setupDiscord();
    me = await api('/api/me');
    if (me.wallet) showMintHome();
    else {
      status(`Hey ${me.username} — sign in with Xaman to start building.`);
      await startSignin();
    }
  } catch (e) {
    console.error(e);
    status(`Failed to connect: ${e.message}`);
  }
}

main();
