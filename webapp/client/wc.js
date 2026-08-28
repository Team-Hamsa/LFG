// wc.js — WalletConnect v2 / Joey Wallet plumbing (#447).
//
// Lazily imported by app.js ONLY on a Joey path (sign-in, wallet linking, or
// signing an `lfg-wc://` request): the vendored bundle is ~600 KB, and a
// Xaman user must never pay for it. Everything DOM-free lives in
// signdelivery_pure.js; this module owns the client singleton, the pairing
// session and the one JSON-RPC method Joey speaks.

const TOPIC_KEY = 'lfg_wc_topic';
const XRPL_METHOD = 'xrpl_signTransaction';

let client = null;      // SignClient singleton
let topic = null;       // live session topic
let wallet = null;      // XRPL classic address of the connected account
let modal = null;       // WalletConnectModal singleton

function storeTopic(t) {
  topic = t;
  try {
    if (t) localStorage.setItem(TOPIC_KEY, t);
    else localStorage.removeItem(TOPIC_KEY);
  } catch (_) { /* private mode */ }
}

function storedTopic() {
  try { return localStorage.getItem(TOPIC_KEY); } catch (_) { return null; }
}

// "xrpl:0:rXXXX" -> "rXXXX"
function accountOf(session) {
  const accounts = ((session || {}).namespaces || {}).xrpl?.accounts || [];
  const first = accounts[0];
  return typeof first === 'string' ? first.split(':')[2] || null : null;
}

function adopt(session) {
  storeTopic(session.topic);
  wallet = accountOf(session);
  return { wallet, topic };
}

async function ensureClient({ projectId, metadata }) {
  if (client) return client;
  const { SignClient } = await import('./vendor/walletconnect.js?v=1');
  client = await SignClient.init({ projectId, metadata });
  // The wallet may drop the pairing from its side at any time — forget it so
  // the next connect() opens a fresh modal instead of signing into a corpse.
  client.on('session_delete', (e) => {
    if (!e || e.topic === topic) { storeTopic(null); wallet = null; }
  });
  return client;
}

function liveSession(c, t) {
  if (!t) return null;
  try { return c.session.get(t) || null; } catch (_) { return null; }
}

// Connect (or re-attach) a Joey session. Resolves to {wallet, topic}.
//   fresh: true forces a NEW pairing even when a live session exists — the
//          wallet-link flow needs the user to bring a DIFFERENT wallet.
export async function connect({ projectId, chain, metadata, fresh = false } = {}) {
  const c = await ensureClient({ projectId, metadata });
  if (!fresh) {
    const existing = liveSession(c, topic || storedTopic());
    if (existing) return adopt(existing);
  }
  const { uri, approval } = await c.connect({
    requiredNamespaces: {
      xrpl: { chains: [chain], methods: [XRPL_METHOD], events: [] },
    },
  });
  if (!modal) {
    const { WalletConnectModal } = await import('./vendor/walletconnect.js?v=1');
    modal = new WalletConnectModal({ projectId, chains: [chain] });
  }
  if (uri) await modal.openModal({ uri });
  try {
    const session = await approval();
    return adopt(session);
  } finally {
    try { modal.closeModal(); } catch (_) { /* already closed */ }
  }
}

// Re-attach a stored session without ever opening the modal. Returns the
// wallet on success, or null when the pairing is gone (the caller then falls
// back to a fresh sign-in).
export async function restore({ projectId, metadata } = {}) {
  const t = storedTopic();
  if (!t) return null;
  const c = await ensureClient({ projectId, metadata });
  const session = liveSession(c, t);
  if (!session) { storeTopic(null); return null; }
  return adopt(session).wallet;
}

// Ask Joey to sign (and optionally submit) a transaction. Returns the raw
// response `{tx_json, hash?}`; a user rejection throws the WalletConnect
// JSON-RPC error unchanged (signdelivery_pure.isWcRejection classifies it).
export async function signTx({ chain, txJson, autofill = true, submit = true } = {}) {
  if (!client || !topic) throw new Error('no Joey Wallet session');
  return client.request({
    topic,
    chainId: chain,
    request: { method: XRPL_METHOD, params: { tx_json: txJson, options: { autofill, submit } } },
  });
}

export async function disconnect() {
  const t = topic;
  storeTopic(null);
  wallet = null;
  if (!client || !t) return;
  try {
    await client.disconnect({ topic: t, reason: { code: 6000, message: 'user disconnected' } });
  } catch (_) { /* the session was already gone */ }
}

export function activeWallet() {
  return wallet;
}
