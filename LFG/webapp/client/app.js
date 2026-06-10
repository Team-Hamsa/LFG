// LFG Discord Activity frontend.
//
// Inside Discord the page is served through the Activity proxy; the SDK is
// loaded via the "/.proxy/esm" URL mapping (-> esm.sh, see docs/ACTIVITY_SETUP.md).
// Outside Discord (no frame_id query param) it runs in a degraded dev mode
// without Discord auth, so the API will return 401 — useful only for UI work.

const params = new URLSearchParams(window.location.search);
const insideDiscord = params.has('frame_id');

const el = (id) => document.getElementById(id);
const status = (msg) => { el('status').textContent = msg; };

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

function openExternal(url) {
  if (externalOpener) externalOpener(url);
  else window.open(url, '_blank');
}

async function setupDiscord() {
  // The SDK must be reached through the Activity proxy URL mapping.
  const { DiscordSDK } = await import('/.proxy/esm/@discord/embedded-app-sdk');
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

function showFlow({ title, text, qrData, link, image, done }) {
  showPanel('flow-panel');
  el('flow-title').textContent = title;
  el('flow-text').textContent = text || '';
  el('flow-qr').hidden = !qrData;
  if (qrData) el('flow-qr').src = qrUrl(qrData);
  el('flow-link-btn').hidden = !link;
  if (link) el('flow-link-btn').onclick = () => openExternal(link);
  el('nft-image').hidden = !image;
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

async function pollMint(sessionId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    let s;
    try {
      s = await api(`/api/mint/${sessionId}`);
    } catch (e) {
      return; // transient; keep polling
    }

    if (s.state === 'awaiting_payment') {
      const [title, text] = STAGE_TEXT.awaiting_payment;
      showFlow({ title, text, qrData: s.payment_link, link: s.payment_link });
    } else if (STAGE_TEXT[s.state]) {
      const [title, text] = STAGE_TEXT[s.state];
      showFlow({ title, text });
    } else if (s.state === 'offer_ready') {
      clearInterval(pollTimer);
      showFlow({
        title: `🎉 NFT #${s.nft_number} minted!`,
        text: 'Scan the QR (or open in Xaman) to accept the offer and claim your NFT.',
        qrData: s.accept_deeplink,
        link: s.accept_deeplink,
        image: s.image_url,
        done: true,
      });
    } else if (s.state === 'payment_timeout') {
      clearInterval(pollTimer);
      showFlow({ title: '⏰ Payment timed out', text: 'No payment was received in time. Try again.', done: true });
    } else if (s.state === 'failed') {
      clearInterval(pollTimer);
      showFlow({ title: '❌ Mint failed', text: s.error || 'Something went wrong.', done: true });
    }
  }, 3000);
}

async function startMint() {
  try {
    const s = await api('/api/mint', { method: 'POST' });
    const [title, text] = STAGE_TEXT.awaiting_payment;
    showFlow({ title, text, qrData: s.payment_link, link: s.payment_link });
    pollMint(s.id);
  } catch (e) {
    status(e.message);
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
    status(e.message);
  }
}

async function registerWallet() {
  const wallet = el('wallet-input').value.trim();
  try {
    await api('/api/register', { method: 'POST', body: JSON.stringify({ wallet }) });
    me.wallet = wallet;
    showMintHome();
  } catch (e) {
    status(e.message);
  }
}

// --- Trait Swapper ---

let swapNfts = [];
let swapPick = [];
let swapPollTimer = null;

const showSwapPanel = showPanel;

async function openSwapper() {
  showSwapPanel('swap-panel');
  swapPick = [];
  el('nft-grid').innerHTML = '';
  status('Loading your NFTs…');
  try {
    const data = await api('/api/nfts');
    swapNfts = data.nfts;
    status('');
    if (!swapNfts.length) {
      el('swap-help').textContent = 'No swappable NFTs found in your wallet.';
      return;
    }
    el('swap-help').textContent =
      'Pick two NFTs with the same body type to swap traits between them.';
    for (const nft of swapNfts) {
      const card = document.createElement('button');
      card.className = 'nft-card';
      card.innerHTML = `<img src="${nft.image}" alt=""><span>${nft.name}</span>`;
      card.onclick = () => toggleNftPick(nft, card);
      el('nft-grid').appendChild(card);
    }
  } catch (e) {
    status(e.message);
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
  showSwapPanel('swap-traits-panel');
  el('swap-img1').src = a.image;
  el('swap-img2').src = b.image;
  el('swap-name1').textContent = a.name;
  el('swap-name2').textContent = b.name;
  const list = el('trait-list');
  list.innerHTML = '';
  const swappable = ['Background', 'Back', 'Clothing', 'Mouth',
                     'Eyebrows', 'Eyes', 'Head', 'Accessory'];
  for (const trait of swappable) {
    const row = document.createElement('label');
    row.className = 'trait-row';
    row.innerHTML = `<input type="checkbox" value="${trait}">
      <strong>${trait}</strong>
      <span>${traitValue(a, trait)} ↔ ${traitValue(b, trait)}</span>`;
    list.appendChild(row);
  }
}

const SWAP_STAGE_TEXT = {
  composing: ['🎨 Crafting new NFTs', 'Composing the swapped images…'],
  uploading: ['☁️ Uploading', 'Saving the new images and metadata to the CDN…'],
  burning: ['🔥 Burning originals', 'Burning the two original NFTs on XRPL…'],
  minting: ['⛏️ Reminting', 'Minting the re-crafted NFTs…'],
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
    showSwapPanel('swap-result-panel');
    el('swap-results').innerHTML = '';
    el('swap-done-btn').hidden = true;
    pollSwap(s.id);
  } catch (e) {
    status(e.message);
  }
}

function renderSwapProgress(state) {
  const [title, text] = SWAP_STAGE_TEXT[state] || ['Working…', ''];
  el('swap-result-title').textContent = title;
  el('swap-result-text').textContent = text;
}

function renderSwapResults(s) {
  el('swap-result-title').textContent = '🎉 New NFTs crafted!';
  el('swap-result-text').textContent =
    'Scan each QR (or open in Xaman) to accept your re-crafted NFTs.';
  const box = el('swap-results');
  box.innerHTML = '';
  for (const r of s.results) {
    const div = document.createElement('div');
    div.className = 'swap-result';
    div.innerHTML = `<h3>${r.name}</h3>
      <img class="result-img" src="${r.image_url}" alt="">
      <img class="result-qr" src="${qrUrl(r.accept_deeplink)}" alt="QR">`;
    const btn = document.createElement('button');
    btn.className = 'link';
    btn.textContent = 'Open in Xaman';
    btn.onclick = () => openExternal(r.accept_deeplink);
    div.appendChild(btn);
    box.appendChild(div);
  }
  el('swap-done-btn').hidden = false;
}

async function pollSwap(sessionId) {
  clearInterval(swapPollTimer);
  swapPollTimer = setInterval(async () => {
    let s;
    try { s = await api(`/api/swap/${sessionId}`); } catch (e) { return; }
    if (SWAP_STAGE_TEXT[s.state]) {
      renderSwapProgress(s.state);
    } else if (s.state === 'offers_ready') {
      clearInterval(swapPollTimer);
      renderSwapResults(s);
    } else if (s.state === 'failed') {
      clearInterval(swapPollTimer);
      el('swap-result-title').textContent = '❌ Swap failed';
      el('swap-result-text').textContent = s.error || 'Something went wrong.';
      el('swap-done-btn').hidden = false;
    }
  }, 3000);
}

async function main() {
  el('register-btn').onclick = registerWallet;
  el('mint-btn').onclick = startMint;
  el('trustline-btn').onclick = startTrustline;
  el('swap-btn').onclick = openSwapper;
  el('swap-back-btn').onclick = () => showMintHome();
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
