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

function showPanel(id) {
  for (const panel of ['register-panel', 'mint-panel', 'flow-panel']) {
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

async function main() {
  el('register-btn').onclick = registerWallet;
  el('mint-btn').onclick = startMint;
  el('trustline-btn').onclick = startTrustline;
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
