// LFG Discord Activity frontend.
//
// Inside Discord the page is served through the Activity proxy; the SDK is
// vendored same-origin at vendor/embedded-app-sdk.js (see docs/ACTIVITY_SETUP.md).
// Outside Discord (no frame_id query param) it runs in a degraded dev mode
// without Discord auth, so the API will return 401 — useful only for UI work.

const params = new URLSearchParams(window.location.search);
const insideDiscord = params.has('frame_id');

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

function openExternal(url) {
  if (externalOpener) externalOpener(url);
  else window.open(url, '_blank');
}

async function setupDiscord() {
  // SDK is vendored same-origin (webapp/client/vendor/) to avoid esm.sh's
  // root-absolute re-exports, which break under the Activity's /.proxy sub-path.
  const { DiscordSDK } = await import('./vendor/embedded-app-sdk.js');
  const { client_id: clientId } = await api('/api/config');
  const sdk = new DiscordSDK(clientId);
  await sdk.ready();

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

const ALL_PANELS = ['register-panel', 'mint-panel', 'flow-panel',
                    'swap-panel', 'swap-traits-panel', 'swap-result-panel'];

function showPanel(id) {
  for (const panel of ALL_PANELS) {
    el(panel).hidden = panel !== id;
  }
}

function showMintHome() {
  el('wallet-display').textContent = me.wallet;
  showPanel('mint-panel');
  status(`Hey ${me.username}! Ready to mint.`);
}

// Mint flow step indicator (hidden for flows without a stage, e.g. trustlines)
const MINT_STEPS = ['Pay', 'Generate', 'Mint', 'Claim'];
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

function showFlow({ title, text, qrData, link, image, done, stage, spinner, celebrate }) {
  showPanel('flow-panel');
  renderSteps(stage);
  el('flow-title').textContent = title;
  el('flow-text').textContent = text || '';
  el('flow-spinner').hidden = !spinner;
  el('flow-qr').hidden = !qrData;
  if (qrData) el('flow-qr').src = qrUrl(qrData);
  el('flow-link-btn').hidden = !link;
  if (link) el('flow-link-btn').onclick = () => openExternal(link);
  el('nft-image').hidden = !image;
  el('nft-image').classList.toggle('celebrate', !!(image && celebrate));
  if (image) el('nft-image').src = image;
  el('flow-done-btn').hidden = !done;
}

const STAGE_TEXT = {
  awaiting_payment: ['💰 Payment required',
    'Pay 1 LFGO token to mint. Scan the QR with Xaman/XUMM or open the link, approve, then wait here.'],
  generating: ['🎨 Generating your NFT', 'Payment received! Composing your one-of-a-kind image…'],
  minting: ['⛏️ Minting on XRPL', 'Submitting the NFTokenMint transaction…'],
  creating_offer: ['📨 Creating transfer offer', 'Almost there — preparing the offer to your wallet…'],
};

// Chained setTimeout (not setInterval) so a slow response can never overlap
// the next request or apply stale state out of order.
function pollMint(sessionId) {
  clearTimeout(pollTimer);
  const tick = async () => {
    let s;
    try {
      s = await api(`/api/mint/${sessionId}`);
    } catch (e) {
      pollTimer = setTimeout(tick, 3000); // transient; keep polling
      return;
    }

    if (s.state === 'offer_ready') {
      showFlow({
        title: `🎉 NFT #${s.nft_number} minted!`,
        text: 'Scan the QR (or open in Xaman) to accept the offer and claim your NFT.',
        qrData: s.accept_deeplink,
        link: s.accept_deeplink,
        image: imgUrl(s.image_url),
        done: true,
        stage: s.state,
        celebrate: true,
      });
      return;
    }
    if (s.state === 'payment_timeout') {
      showFlow({ title: '⏰ Payment timed out', text: 'No payment was received in time. Try again.', done: true });
      return;
    }
    if (s.state === 'failed') {
      showFlow({ title: '❌ Mint failed', text: s.error || 'Something went wrong.', done: true });
      return;
    }

    if (s.state === 'awaiting_payment') {
      const [title, text] = STAGE_TEXT.awaiting_payment;
      showFlow({ title, text, qrData: s.payment_link, link: s.payment_link, stage: s.state });
    } else if (STAGE_TEXT[s.state]) {
      const [title, text] = STAGE_TEXT[s.state];
      showFlow({ title, text, stage: s.state, spinner: true });
    }
    pollTimer = setTimeout(tick, 3000);
  };
  pollTimer = setTimeout(tick, 3000);
}

async function startMint() {
  try {
    const s = await api('/api/mint', { method: 'POST' });
    const [title, text] = STAGE_TEXT.awaiting_payment;
    showFlow({ title, text, qrData: s.payment_link, link: s.payment_link, stage: s.state });
    pollMint(s.id);
  } catch (e) {
    showError(e.message);
  }
}

async function startTrustline() {
  try {
    const t = await api('/api/trustline', { method: 'POST' });
    showFlow({
      title: '🔗 Set LFGO Trustline',
      text: 'Scan with Xaman/XUMM and approve the TrustSet. Expires in 5 minutes.',
      qrData: t.xumm_url,
      link: t.xumm_url,
      done: true,
    });
  } catch (e) {
    showError(e.message);
  }
}

async function startBrixTrustline() {
  try {
    const t = await api('/api/brix-trustline', { method: 'POST' });
    showFlow({
      title: '🔗 Set BRIX Trustline',
      text: 'Scan with Xaman/XUMM and approve the TrustSet. Required to pay trait swap fees. Expires in 5 minutes.',
      qrData: t.xumm_url,
      link: t.xumm_url,
      done: true,
    });
  } catch (e) {
    showError(e.message);
  }
}

async function registerWallet() {
  const wallet = el('wallet-input').value.trim();
  try {
    await api('/api/register', { method: 'POST', body: JSON.stringify({ wallet }) });
    me.wallet = wallet;
    showMintHome();
  } catch (e) {
    showError(e.message);
  }
}

// --- Trait Swapper ---

let swapNfts = [];
let swapPick = [];
let swapPollTimer = null;
let swappableTraits = [];

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
  showGridSkeletons(el('nft-grid'));
  status('Loading your NFTs…');
  try {
    const data = await api('/api/nfts');
    swapNfts = data.nfts;
    swappableTraits = data.swappable_traits || [];
    status('');
    el('nft-grid').replaceChildren(); // drop the skeleton loaders
    if (!swapNfts.length) {
      el('swap-help').textContent = 'No swappable NFTs found in your wallet.';
      return;
    }
    el('swap-help').textContent =
      'Pick two NFTs with the same body type to swap traits between them.';
    for (const nft of swapNfts) {
      const card = document.createElement('button');
      card.className = 'nft-card';
      // NFT metadata is untrusted — build DOM nodes, never innerHTML.
      const img = document.createElement('img');
      img.src = imgUrl(nft.image);
      img.alt = '';
      const name = document.createElement('span');
      name.textContent = nft.name;
      card.replaceChildren(img, name);
      card.onclick = () => toggleNftPick(nft, card);
      el('nft-grid').appendChild(card);
    }
  } catch (e) {
    el('nft-grid').replaceChildren(); // drop the skeleton loaders
    showError(e.message);
  }
}

function toggleNftPick(nft, card) {
  const idx = swapPick.findIndex((p) => p.nft.nft_id === nft.nft_id);
  if (idx >= 0) {
    swapPick.splice(idx, 1);
    card.classList.remove('selected');
    return;
  }
  if (swapPick.length === 2) return;
  if (swapPick.length === 1 && swapPick[0].nft.gender !== nft.gender) {
    status('Both NFTs must share the same body type.');
    return;
  }
  swapPick.push({ nft, card });
  card.classList.add('selected');
  status('');
  if (swapPick.length === 2) showTraitChooser();
}

function traitValue(nft, traitType) {
  const a = nft.attributes.find((t) => t.trait_type === traitType);
  return a ? a.value : 'None';
}

function showTraitChooser() {
  const [a, b] = swapPick.map((p) => p.nft);
  showPanel('swap-traits-panel');
  el('swap-img1').src = imgUrl(a.image);
  el('swap-img2').src = imgUrl(b.image);
  el('swap-name1').textContent = a.name;
  el('swap-name2').textContent = b.name;
  const list = el('trait-list');
  list.innerHTML = '';
  for (const trait of swappableTraits) {
    const row = document.createElement('label');
    row.className = 'trait-row';
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
  composing: ['🎨 Crafting new NFTs', 'Composing the swapped images…'],
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
      body: JSON.stringify({ nft1_id: a.nft_id, nft2_id: b.nft_id, traits }),
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
    `Pay ${s.fee_amount} BRIX to swap your mutable NFT(s) in place. ` +
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

async function main() {
  el('register-btn').onclick = registerWallet;
  el('mint-btn').onclick = startMint;
  el('trustline-btn').onclick = startTrustline;
  el('swap-btn').onclick = openSwapper;
  el('swap-back-btn').onclick = () => showMintHome();
  el('brix-trustline-btn').onclick = startBrixTrustline;
  el('swap-cancel-btn').onclick = () => openSwapper();
  el('swap-confirm-btn').onclick = confirmSwap;
  el('swap-done-btn').onclick = () => showMintHome();
  el('change-wallet-btn').onclick = () => { showPanel('register-panel'); };
  el('flow-done-btn').onclick = () => { showMintHome(); };

  if (!insideDiscord) {
    status('Not running inside Discord — open this as an Activity. (Dev mode: API calls will be unauthorized.)');
    return;
  }

  try {
    await setupDiscord();
    me = await api('/api/me');
    if (me.wallet) showMintHome();
    else {
      showPanel('register-panel');
      status(`Hey ${me.username}! Register your XRPL wallet to get started.`);
    }
  } catch (e) {
    console.error(e);
    status(`Failed to connect: ${e.message}`);
  }
}

main();
