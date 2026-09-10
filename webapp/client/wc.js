// wc.js — WalletConnect v2 / Joey Wallet plumbing (#447).
//
// Lazily imported by app.js ONLY on a Joey path (sign-in, wallet linking, or
// signing an `lfg-wc://` request): the vendored bundle is ~600 KB, and a
// Xaman user must never pay for it. Everything DOM-free lives in
// signdelivery_pure.js; this module owns the client singleton, the pairing
// session and the one JSON-RPC method Joey speaks. Pairing UI is the
// caller's: connect() hands the wc: URI to onUri instead of opening the
// stock Reown wallet modal (retired — its QR read as unscannable/foreign to
// Joey users; app.js renders our own branded QR + joey:// deep link).

const TOPIC_KEY = 'lfg_wc_topic';
const XRPL_METHOD = 'xrpl_signTransaction';
let client = null;      // SignClient singleton
let topic = null;       // live session topic
let wallet = null;      // XRPL classic address of the connected account

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
  // the next connect() starts a fresh pairing instead of signing into a corpse.
  client.on('session_delete', (e) => {
    if (!e || e.topic === topic) { storeTopic(null); wallet = null; }
  });
  return client;
}

function liveSession(c, t) {
  if (!t) return null;
  try { return c.session.get(t) || null; } catch (_) { return null; }
}

// A pairing used for ONE transaction and then thrown away: reported to the
// caller without touching the module's session state or localStorage. The
// wallet-link flow pairs a SECOND wallet purely to prove ownership; adopting
// it would silently repoint every later signTx (and the next reload's
// restore) at the wrong account, against the signed-in wallet's LFG session.
function borrow(session) {
  return { wallet: accountOf(session), topic: session.topic };
}

// Close a borrowed one-shot pairing. Best-effort, and hard-guarded against
// ever tearing down the primary session.
export async function release(borrowedTopic) {
  if (!client || !borrowedTopic || borrowedTopic === topic) return;
  try {
    await client.disconnect({
      topic: borrowedTopic,
      reason: { code: 6000, message: 'user disconnected' },
    });
  } catch (_) { /* already gone */ }
}

// Connect (or re-attach) a Joey session. Resolves to {wallet, topic}.
//   fresh: true forces a NEW pairing even when a live session exists — the
//          wallet-link flow needs the user to bring a DIFFERENT wallet. That
//          pairing is BORROWED, never adopted: pass its topic to signTx and
//          hand it to release() when done.
//   onUri: called with the raw `wc:` pairing URI when a NEW pairing is
//          needed (skipped when an existing session is re-attached). The
//          caller renders it — app.js draws the branded /api/qr.png QR and
//          Joey deep link; the stock Reown wallet modal is gone (#447
//          follow-up: its Xaman-styled modal QR confused Joey users).
export async function connect({ projectId, chain, metadata, fresh = false, onUri } = {}) {
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
  if (uri && onUri) onUri(uri);
  const session = await approval();
  return fresh ? borrow(session) : adopt(session);
}

// Re-attach a stored session without ever starting a new pairing. Returns the
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
//
// `topic` overrides the session pairing — that is how a borrowed one-shot
// pairing signs its link proof without ever becoming the session.
export async function signTx({ chain, txJson, autofill = true, submit = true,
                              topic: requestTopic } = {}) {
  const t = requestTopic || topic;
  if (!client || !t) throw new Error('no Joey Wallet session');
  return client.request({
    topic: t,
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
