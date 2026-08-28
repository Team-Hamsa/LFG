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
import * as marketPure from './market_pure.js?v=25';
// Mint-flow pure helpers (issue #141): the cancel-outcome decision lives in
// its own module so it's Node-testable too (tests/test_mint_pure_js.py).
import * as mintPure from './mint_pure.js?v=24';
// Build-panel decision logic lives in its own pure module so it's
// Node-testable too (tests/test_build_pure_js.py).
import * as buildPure from './build_pure.js?v=29';
// Cold-boot session-resume decisions (#221): which live flow to re-attach to
// after a webview relaunch is a pure priority picker, Node-testable
// (tests/test_resume_pure_js.py); resumeAnyFlow() below is the thin DOM glue.
import * as resumePure from './resume_pure.js?v=2';
// Animated-art strategy (#298): grid-vs-detail asset decisions are pure and
// Node-testable (tests/test_media_pure_js.py) — grids always render the
// static image (badged when animated); only detail/focused views upgrade to
// the video.
import * as mediaPure from './media_pure.js?v=2';
// Batch-harvest (#356) selection/summary decisions are pure and Node-testable
// (tests/test_harvest_pure_js.py); the GO-picker multi-select below is the glue.
import * as harvestPure from './harvest_pure.js?v=2';
// Xaman sign-request delivery decisions (#142): mobile-primary deep link vs
// desktop-primary QR is a pure truth table, Node-testable
// (tests/test_signdelivery_pure_js.py); applySignDelivery() below is the glue.
import * as signDeliveryPure from './signdelivery_pure.js?v=2';
// Daily BRIX drip card (#48): what the card renders and how each claim error
// code is handled are pure decisions, Node-testable (tests/test_brix_pure_js.py);
// loadBrix()/claimBrix() below are the glue.
import * as brixPure from './brix_pure.js?v=8';

const params = new URLSearchParams(window.location.search);
const insideDiscord = params.has('frame_id');
// Telegram injects a signed launch payload as Telegram.WebApp.initData; the
// vendored telegram-web-app.js (loaded before this module) defines window.Telegram
// inside Telegram and stays undefined everywhere else.
const tg = window.Telegram && window.Telegram.WebApp;
const insideTelegram = !!(tg && tg.initData);

// Standalone web surface (spec 2026-07-16): config.js sets window.LFG_WEB when
// this client is served from GitHub Pages (build.letseffinggo.com); the API
// then lives on another origin (the funnel) and auth is a Xaman wallet
// sign-in instead of Discord/Telegram. The repo-default config.js keeps
// LFG_WEB null, so nothing changes for the other surfaces.
const webCfg = window.LFG_WEB || null;
const insideWeb = !!webCfg && !insideDiscord && !insideTelegram;
const API_BASE = (webCfg && webCfg.apiBase) || '';
const WEB_SESSION_KEY = 'lfg_web_session';

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
// /api/config, fetched once in main() and kept module-level so the Joey
// Wallet paths (#447) can read cfg.walletconnect without re-fetching. Null
// until that fetch lands (or forever, if it failed) — every read must treat
// a missing config as "feature off".
let appCfg = null;
let me = null;
let pollTimer = null;
let pollGen = 0; // bumps on every pollMint call, invalidating in-flight ticks
// Bumped by EVERY showFlow() render (and invalidateFlowPolls): lets an async
// callback that captured it detect that some other flow has since taken over
// the shared flow-panel (visibility alone can't tell whose panel it is).
let flowRenderGen = 0;
let externalOpener = null; // set when the SDK is available
// "Share on X" (#41 T9): populated from /api/config by BOTH fetch sites —
// main()'s init probe (whose failure is deliberately swallowed) AND
// setupDiscord()'s client_id fetch — so one transient config failure can't
// leave shareUrlFor() emitting dead links for the whole session. NEVER
// derive these from the page's own browser-reported address — inside the
// Activity the page is served from Discord's *.discordsays.com sandbox
// proxy, not our public host, so a link built from that would be dead for
// X's crawler.
let shareBase = '';
let bithompBase = '';
// #252: server-side "Share from my account" (per-user X OAuth). False until
// /api/config reports the feature armed; the Web Intent button is always the
// fallback either way.
let xUserShare = false;

function applyShareConfig(cfg) {
  // Keep an already-populated base if a later fetch omits the field.
  shareBase = (cfg && cfg.public_share_base_url) || shareBase;
  bithompBase = (cfg && cfg.bithomp_base_url) || bithompBase;
  // Boolean, not a string base: the latch-on ||-merge above would make
  // `false` indistinguishable from "field absent" and the feature could
  // never turn off. Only adopt the value when the server actually sent it.
  if (cfg && 'x_user_share' in cfg) xUserShare = !!cfg.x_user_share;
}

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (sessionToken) headers['Authorization'] = `Bearer ${sessionToken}`;
  const res = await fetch(API_BASE + path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // Web surface: an expired/invalid stored session must not survive a
    // reload — drop it so the next boot re-offers the Xaman sign-in.
    if (insideWeb && res.status === 401) {
      try { localStorage.removeItem(WEB_SESSION_KEY); } catch (_) { /* private mode */ }
    }
    const err = new Error(data.error || `HTTP ${res.status}`);
    // Some endpoints (e.g. 409 shop/market session_active) carry extra
    // fields (code, session_id) callers need to resume rather than just
    // display — attach the full body without changing .message so every
    // existing `e.message === '...'` check keeps working unmodified.
    err.status = res.status;
    err.body = data;
    throw err;
  }
  return data;
}

function qrUrl(data) {
  return `${API_BASE}/api/qr.png?d=${encodeURIComponent(data)}`;
}

// CDN images are cross-origin and blocked by the Activity's CSP, so they are
// routed through the backend's same-origin proxy (like the QR codes).
// Grid/roster tiles pass THUMB_W: the proxy then serves a pre-built ~10 KB
// 256px WebP instead of the ~634 KB full still (falling back to the full
// image when no thumb exists, so passing it is always safe).
const THUMB_W = 256;
function imgUrl(url, w) {
  if (!url) return url;
  const base = `${API_BASE}/api/img?u=${encodeURIComponent(url)}`;
  return w ? `${base}&w=${w}` : base;
}

// Animated NFTs (#250) ship an .mp4 next to the PNG poster frame. Where a
// video URL is present, full-size artwork renders as <video autoplay loop
// muted playsinline> with the still as poster; otherwise the usual <img>.
// The video src goes through the same /api/img proxy as the stills (CSP:
// the CDN is cross-origin) — the proxy passes .mp4 through untouched, and
// its w= resize only applies to archived stills, so it is never sent for
// the video itself.
function mediaEl({ image, video, thumbW, className, alt }) {
  const m = document.createElement(video ? 'video' : 'img');
  if (className) m.className = className;
  if (video) {
    m.muted = true;
    // Attributes, not just properties: webview autoplay policies check them.
    m.setAttribute('muted', '');
    m.setAttribute('autoplay', '');
    m.setAttribute('loop', '');
    m.setAttribute('playsinline', ''); // iOS: play inline, not fullscreen
    // <video> has no alt: carry the label as the accessible name so a later
    // video->img rebuild in setMedia can round-trip it losslessly.
    if (alt) m.setAttribute('aria-label', alt);
    // #298: lazy — fetch only what's needed to start playback (autoplay then
    // pulls the rest); until data arrives the poster still shows instantly.
    m.preload = 'metadata';
    if (image) m.poster = imgUrl(image, thumbW);
    // #298: a failed/undecodable video degrades to the static still instead
    // of a dead black box. The fallback reads the element's CURRENT
    // poster/label — setMedia reuses this element across renders, so
    // creation-time closure values would resurrect the previous NFT's still.
    // id/class/hidden carry over so fixed-id callers keep addressing the slot.
    m.onerror = () => {
      const fb = mediaPure.videoFallback(m.poster, m.getAttribute('aria-label'));
      if (!fb) return; // no still to degrade to — keep the video element
      const still = document.createElement('img');
      still.id = m.id;
      still.className = m.className;
      still.hidden = m.hidden;
      still.src = fb.src;
      still.alt = fb.alt;
      m.replaceWith(still);
    };
    m.src = imgUrl(video);
  } else {
    m.src = imgUrl(image, thumbW);
    m.alt = alt || '';
  }
  return m;
}

// Point a fixed-id artwork slot (mint/assemble hero, swap chooser sides) at a
// piece, swapping the element between <img> and <video> as needed while
// keeping id/class/hidden so the rest of the code can keep addressing it.
function setMedia(id, { image, video, thumbW }) {
  const old = el(id);
  if ((old.tagName === 'VIDEO') === !!video) {
    // Same tag: update in place, and only on change — the mint poller repaints
    // every few seconds and resetting src would restart video playback.
    const src = video ? imgUrl(video) : imgUrl(image, thumbW);
    if (old.getAttribute('src') !== src) {
      // #298: refresh the poster with the render — and CLEAR a stale one
      // when the new piece has no still, so the error fallback (which reads
      // the current poster) can never show the previous NFT's art.
      if (video) {
        if (image) old.poster = imgUrl(image, thumbW);
        else old.removeAttribute('poster');
        // The accessible label was baked in at creation for a different
        // piece; without a fresh one, stale is worse than none — the error
        // fallback reads it as the replacement image's alt.
        old.removeAttribute('aria-label');
      }
      old.src = src;
    }
    // Re-arm playback: autoplay only fires on load, and showFlow pauses the
    // hero while it's hidden — an unchanged src would otherwise stay frozen.
    if (video && old.paused) old.play().catch(() => {});
    return old;
  }
  const fresh = mediaEl({
    image, video, thumbW,
    className: old.className,
    alt: old.getAttribute('alt') || old.getAttribute('aria-label') || '',
  });
  fresh.id = id;
  fresh.hidden = old.hidden;
  // Stop decoding before detaching — avoids Chrome's "play() request was
  // interrupted" warning when an in-flight play() promise is still pending.
  if (old.tagName === 'VIDEO') old.pause();
  old.replaceWith(fresh);
  return fresh;
}

// Guild/channel hosting the Activity; the backend turns these into a XUMM
// return_url so Xaman's post-sign button bounces back into Discord.
function discordCtx() {
  return {
    guild_id: params.get('guild_id'),
    channel_id: params.get('channel_id'),
  };
}

// #273: the share click-through ref stashed by main() (localStorage lfg_ref,
// shape-validated on write, stored as {ref, ts}). Sent with a mint start so
// the service can attribute the mint; the server re-validates and rejects
// self-referrals. Attribution window (Greptile P1s on PR #393): the stash
// expires REF_TTL_MS after the click, and consumeRef() clears it only once
// the status poll observes a MINTED outcome (single: offer_ready; bulk:
// minted > 0) — a timed-out/cancelled/failed attempt keeps the click's
// attribution for the retry, while the first mint that actually records a
// referrer stops later mints re-attributing the same click. Idempotent; the
// server records per-mint whatever ref the start carried, and one active
// session per user means no overlapping starts.
const REF_TTL_MS = 24 * 60 * 60 * 1000; // 24h click->mint attribution window

function stashedRef() {
  try {
    const raw = localStorage.getItem('lfg_ref');
    if (!raw) return null;
    let ref = raw;
    let ts = null;
    if (raw[0] === '{') {
      const o = JSON.parse(raw);
      ref = o && o.ref;
      ts = o && o.ts;
    }
    // Legacy plain-string stashes (pre-window) have no timestamp: age is
    // unknown, so treat them as expired rather than attribute a mint to an
    // arbitrarily old click.
    if (typeof ts !== 'number' || Date.now() - ts > REF_TTL_MS) {
      localStorage.removeItem('lfg_ref');
      return null;
    }
    return typeof ref === 'string' && XRPL_ADDR_RE.test(ref) ? ref : null;
  } catch (_) { return null; }
}

function consumeRef() {
  try { localStorage.removeItem('lfg_ref'); } catch (_) { /* no storage */ }
}

function openExternal(url) {
  // Returns the launch result so callers can detect a blocked window.open
  // (null). The Discord SDK opener's outcome is genuinely undetectable (it
  // returns a promise that resolves either way) — callers must treat only an
  // explicit null as "blocked".
  if (externalOpener) return externalOpener(url);
  return window.open(url, '_blank');
}

// --- Xaman sign-request delivery (#142) --------------------------------
//
// On a touch-primary device the user's Xaman is (almost always) on THIS
// device — the deep link is the primary path (tap / auto-open once), and the
// QR collapses behind a "sign on another device" disclosure. On desktop the
// QR stays primary exactly as before. Decision logic lives in
// signdelivery_pure.js; this is the DOM glue.

let coarsePointerMql = null;
function isCoarsePointer() {
  if (coarsePointerMql === null) {
    coarsePointerMql = window.matchMedia
      ? window.matchMedia('(pointer: coarse)')
      : { matches: false };
  }
  return !!coarsePointerMql.matches;
}

// Auto-open fires at most once per unique payload link — flow panels
// re-render on every status poll, and each poll must NOT re-launch Xaman.
// Marked optimistically, then un-marked when the launch is DETECTABLY
// blocked (window.open returning null in a standalone browser) so the next
// poll render retries; the Discord SDK opener gives no success signal, so
// there the optimistic mark stands (the primary button remains the
// guaranteed path either way).
let autoOpenedLinks = [];
function maybeAutoOpen(link) {
  if (!signDeliveryPure.shouldAutoOpen(autoOpenedLinks, link)) return;
  autoOpenedLinks.push(link);
  const launched = openExternal(link);
  autoOpenedLinks = signDeliveryPure.autoOpenOutcome(autoOpenedLinks, link, launched !== null);
}

// "Show QR to sign on another device" disclosure button for dynamically
// built sign panels; hidden until applySignDelivery collapses the QR.
function makeQrToggle() {
  const btn = document.createElement('button');
  btn.className = 'link qr-toggle';
  btn.textContent = 'Show QR to sign on another device';
  btn.hidden = true;
  return btn;
}

// Single choke-point wiring a sign panel's QR <img> + "Open in Xaman"
// button (+ optional disclosure toggle) per the signDelivery truth table.
// Pass autoOpen:false for panels reached passively (not a fresh sign ask).
function applySignDelivery({ qrEl, linkBtn, toggleBtn, link, qrData, push, autoOpen = true }) {
  // #447: a WalletConnect sign request has no QR and no deep link — the
  // transaction goes to Joey over the live session instead. Every flow keeps
  // calling this exactly as before; only the rendering differs.
  if (signDeliveryPure.isWcLink(link)) {
    const rid = signDeliveryPure.wcRequestId(link);
    if (qrEl) qrEl.hidden = true;
    if (toggleBtn) toggleBtn.hidden = true;
    // The panel's "Open in Xaman" button is the one affordance every sign
    // screen already has, so it doubles as the retry after a Joey run failed
    // without recording an outcome (modal dismissed, relay down). Its original
    // label is stashed so a later Xaman render restores it.
    if (linkBtn) {
      const failed = wcFailed.has(rid);
      linkBtn.hidden = !failed;
      if (failed) {
        if (linkBtn.dataset.wcLabel === undefined) linkBtn.dataset.wcLabel = linkBtn.textContent;
        linkBtn.textContent = '🔁 Retry Joey';
        linkBtn.onclick = () => wcRetry(rid);
      }
    }
    wcSign(rid);
    return { linkPrimary: false, qrCollapsed: true, autoOpen: false };
  }
  const d = signDeliveryPure.signDelivery({
    push,
    coarse: isCoarsePointer(),
    hasLink: !!link,
    hasQr: !!qrData,
  });
  if (linkBtn) {
    // Undo a "Retry Joey" relabel if this panel ever renders a Xaman link.
    if (linkBtn.dataset.wcLabel !== undefined) {
      linkBtn.textContent = linkBtn.dataset.wcLabel;
      delete linkBtn.dataset.wcLabel;
    }
    linkBtn.hidden = !link;
    if (link) linkBtn.onclick = () => openExternal(link);
    linkBtn.classList.toggle('sign-primary', d.linkPrimary);
  }
  if (qrEl) {
    qrEl.hidden = !qrData || d.qrCollapsed;
    if (qrData) qrEl.src = qrUrl(qrData);
  }
  if (toggleBtn) {
    toggleBtn.hidden = !d.qrCollapsed;
    toggleBtn.onclick = () => {
      toggleBtn.hidden = true;
      if (qrEl) qrEl.hidden = false;
    };
  }
  if (autoOpen && d.autoOpen) maybeAutoOpen(link);
  return d;
}

// --- WalletConnect / Joey Wallet (#447) --------------------------------
//
// A fourth signing path alongside Xaman's QR/deep link: the user pairs their
// Joey Wallet once, and every later sign request is pushed down that live
// WalletConnect session instead of rendering a QR. The 600 KB vendored bundle
// is behind a dynamic import so a Xaman user never loads it.
//
// Scope in v1: Joey SIGN-IN and Joey SIGNING are both web-only — only a web
// sign-in mints the provider="walletconnect" session the server dispatches
// on. WC_SURFACES gates the link panel's Joey arm (a proof, not a session)
// on the other surfaces it names.

const WC_MODULE = './wc.js?v=1';
const WC_POLL_MS = 3000;

function wcSurface() {
  return insideTelegram ? 'telegram' : insideWeb ? 'web' : 'discord-activity';
}

// The /api/config walletconnect block, but only when this surface is one the
// operator enabled. Null (feature off) whenever the config never arrived.
function wcConfig() {
  const wc = appCfg && appCfg.walletconnect;
  if (!wc || !wc.project_id || !wc.chain) return null;
  const surfaces = Array.isArray(wc.surfaces) ? wc.surfaces : [];
  return surfaces.includes(wcSurface()) ? wc : null;
}

// Reveal the Joey / link-wallet entry points once /api/config has landed.
// Both nodes are hidden in the markup, so a failed config fetch (or a cached
// older index.html) simply leaves today's Xaman-only UI.
function applyWcVisibility() {
  // v1 ruling: Joey SIGN-IN and Joey TRANSACTION SIGNING are both web-only
  // (the wallet IS the login there, and only a web sign-in mints the
  // provider="walletconnect" session the server dispatches on). What
  // WC_SURFACES still gates elsewhere is the link panel's Joey arm — a proof
  // signature, not a session.
  const wcBtn = el('register-wc-btn');
  if (wcBtn) wcBtn.hidden = !(insideWeb && wcConfig());
  // Linking is a multi-wallet convenience for the wallet-is-login surfaces;
  // the Xaman arm is always available, so this does not depend on wcConfig().
  const linkBtn = el('link-wallet-btn');
  if (linkBtn) linkBtn.hidden = !(insideWeb || insideTelegram);
}

// Wallet-facing app identity shown in Joey's pairing prompt. Deliberately a
// fixed public host, never the page's own reported origin: inside the Discord
// Activity the page is served from Discord's *.discordsays.com sandbox proxy, so an
// origin-derived name/icon would be both wrong and unreachable.
const WC_APP_URL = 'https://build.letseffinggo.com';

function wcMetadata() {
  return {
    name: 'LFG',
    description: "Let's Effing Go — mint and trade NFTs on the XRP Ledger",
    url: WC_APP_URL,
    icons: [`${WC_APP_URL}/assets/icon-192.png`],
  };
}

function loadWc() {
  return import(WC_MODULE);
}

// Drop any Joey pairing before a fresh sign-in, so "change wallet" can switch
// Joey ACCOUNTS instead of silently re-attaching the old one. The module (and
// its 600 KB bundle) is imported only when a topic is actually stored, so a
// Xaman user's "change" never pulls it in.
async function wcSignOut() {
  let hasTopic = false;
  try { hasTopic = !!localStorage.getItem('lfg_wc_topic'); } catch (_) { /* private mode */ }
  if (!hasTopic) return;
  try {
    const mod = await loadWc();
    await mod.disconnect(); // also clears lfg_wc_topic
  } catch (e) {
    console.error(e);
    // The bundle or the relay is unreachable — forget the topic anyway, or
    // the next sign-in silently re-attaches the wallet being replaced.
    try { localStorage.removeItem('lfg_wc_topic'); } catch (_) { /* private mode */ }
  }
}

const wcSleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The proof transaction is built client-side from the server's canonical
// pieces (nonce-bearing memos + SourceTag); it is NEVER submitted — Joey
// signs it with autofill/submit off and the service verifies the signature.
function wcProofTx(wallet, start) {
  return {
    TransactionType: 'AccountSet',
    Account: wallet,
    Fee: '0',
    Sequence: 0,
    LastLedgerSequence: 0,
    SourceTag: start.source_tag,
    Memos: start.memos,
  };
}

// Report a sign request's outcome. A 202 (transaction not yet visible
// on-ledger) and a 503 (we could not reach the ledger) are both "ask again",
// not failures — retry until the request's own expiry.
async function postSignResult(id, body, expiresAt) {
  const path = `/api/sign/${encodeURIComponent(id)}/result`;
  for (;;) {
    let r;
    try {
      r = await api(path, { method: 'POST', body: JSON.stringify(body) });
    } catch (e) {
      const body = e.body || {};
      if (body.code === 'tx_mismatch') {
        showError('Joey signed a different transaction — aborted.');
      }
      // A terminal refusal (409 already_resolved / 409 tx_mismatch / 410
      // expired) IS an answer — return it so the caller retires the id
      // instead of re-posting the outcome forever.
      if (signDeliveryPure.wcOutcomeTerminal(body)) return body;
      if (body.code === 'ledger_unavailable' && Date.now() / 1000 < expiresAt) {
        await wcSleep(WC_POLL_MS);
        continue;
      }
      showError(e.message);
      return null;
    }
    if (r && r.state === 'pending' && r.code === 'tx_not_found') {
      if (Date.now() / 1000 >= expiresAt) return null;
      await wcSleep(WC_POLL_MS);
      continue;
    }
    return r;
  }
}

// Drive one `lfg-wc://` sign request to a recorded outcome.
//
// applySignDelivery is called from every flow poller's re-render, so wcSign
// runs on EVERY tick for a live request. Three module Sets and one Map keep
// that from turning into a loop — or into a wedge:
//   wcInFlight       — a run is already under way for this id;
//   wcDone           — the request is RESOLVED SERVER-SIDE (the GET said
//                      non-pending, or a result POST came back terminal), so
//                      stop even issuing the GET;
//   wcFailed         — a run failed without recording an outcome (modal
//                      dismissed, relay unreachable, lost pairing). The row is
//                      still pending, so without this the next tick would
//                      re-open the modal and re-toast, forever;
//   wcPendingResults — id -> {outcome, expiresAt} for a signature/rejection we
//                      HAVE but could not report yet (the POST failed in
//                      transit, or gave up on a non-terminal 202/503). The row
//                      is still pending, so the id must NOT be retired — but
//                      the transaction may already be on-ledger, so the next
//                      tick must RE-POST the stored outcome, never re-sign.
const wcInFlight = new Set();
const wcDone = new Set();
const wcFailed = new Set();
const wcPendingResults = new Map();
// How long past a request's own expiry to keep re-posting an unreported
// outcome before handing the user the retry affordance.
const WC_REPORT_GRACE_SECONDS = 60;

// Retry a request the user was told had failed. When an outcome is already
// stored, wcSign re-posts THAT — a retry must never produce a second
// signature for a transaction that may already have been submitted.
function wcRetry(id) {
  wcFailed.delete(id);
  wcSign(id);
}

function wcSignFailed(id, msg) {
  wcFailed.add(id);
  showError(`${msg} — press “Retry Joey” to try again.`);
}

// Report an outcome we hold, and retire the id ONLY on a terminal answer.
async function wcReportOutcome(id, outcome, expiresAt) {
  wcPendingResults.set(id, { outcome, expiresAt });
  const answer = await postSignResult(id, outcome, expiresAt);
  if (signDeliveryPure.wcOutcomeTerminal(answer)) {
    wcPendingResults.delete(id);
    wcDone.add(id);
    return;
  }
  // Still unreported. The outcome stays for the next tick to re-post; past the
  // grace window the automatic retries stop and the user drives — the entry is
  // deliberately KEPT so "Retry Joey" re-posts instead of re-signing.
  if (Date.now() / 1000 > expiresAt + WC_REPORT_GRACE_SECONDS) {
    wcSignFailed(id, 'Could not report the result to LFG');
  }
}

async function wcSign(id) {
  if (!id || wcInFlight.has(id) || wcDone.has(id) || wcFailed.has(id)) return;
  wcInFlight.add(id);
  try {
    // An outcome we already hold outranks everything: re-post it, never sign
    // again (the transaction may already be on-ledger).
    const held = wcPendingResults.get(id);
    if (held) { await wcReportOutcome(id, held.outcome, held.expiresAt); return; }

    const wc = wcConfig();
    if (!wc) { wcSignFailed(id, 'Joey Wallet is not available here'); return; }
    const r = await api(`/api/sign/${encodeURIComponent(id)}`);
    if (r.state !== 'pending') { wcDone.add(id); return; } // signed/rejected/expired
    const mod = await loadWc();
    if (!mod.activeWallet()) {
      // The pairing was lost (reload, wallet-side disconnect) — re-attach or
      // ask for a fresh one before there is anything to sign.
      await mod.connect({ projectId: wc.project_id, chain: wc.chain, metadata: wcMetadata() });
    }
    toast('\u{1F4F2} Approve in Joey Wallet…');
    let outcome;
    try {
      const resp = await mod.signTx({
        chain: wc.chain, txJson: r.txjson, autofill: true, submit: true,
      });
      outcome = signDeliveryPure.wcResultAction(resp);
    } catch (e) {
      outcome = signDeliveryPure.isWcRejection(e)
        ? { rejected: true }
        : { error: String((e && e.message) || e) };
    }
    await wcReportOutcome(id, outcome, r.expires_at);
  } catch (e) {
    // Nothing was signed and nothing posted: the row is still pending and the
    // poller will call back next tick, so park it until the user retries.
    wcSignFailed(id, e.message || String(e));
  } finally {
    wcInFlight.delete(id);
  }
}

// Sign in with Joey: a server nonce, signed into a never-submitted proof
// transaction, redeemed for the same platform="web" session the Xaman arm
// issues.
async function startWcSignin() {
  const wc = wcConfig();
  // Web only in v1: the other surfaces authenticate through Discord/Telegram
  // and only USE a Joey pairing for signing, never to establish the session.
  if (!wc || !insideWeb) { showError('Joey Wallet sign-in is not available here.'); return; }
  clearTimeout(signinPollTimer);
  showPanel('register-panel');
  renderSignin({ sub: 'Opening Joey Wallet…', spinner: true });
  try {
    const start = await api('/api/web/signin', {
      method: 'POST',
      body: JSON.stringify({ provider: 'walletconnect' }),
    });
    const mod = await loadWc();
    const { wallet } = await mod.connect({
      projectId: wc.project_id, chain: wc.chain, metadata: wcMetadata(),
    });
    if (!wallet) throw new Error('Joey Wallet did not share an XRPL account.');
    renderSignin({ sub: 'Approve the sign-in request in Joey Wallet…', spinner: true });
    const resp = await mod.signTx({
      chain: wc.chain, txJson: wcProofTx(wallet, start), autofill: false, submit: false,
    });
    const s = await api('/api/web/signin/proof', {
      method: 'POST',
      body: JSON.stringify({ sign_id: start.sign_id, tx_json: resp.tx_json }),
    });
    sessionToken = s.session_token;
    try { localStorage.setItem(WEB_SESSION_KEY, s.session_token); } catch (_) { /* private mode */ }
    me = { ...s.user, wallet: s.wallet };
    showMintHome();
  } catch (e) {
    if (signDeliveryPure.isWcRejection(e)) {
      renderSignin({ sub: 'Sign-in declined in Joey Wallet.', retry: true });
      return;
    }
    showError(e.message || String(e));
    renderSignin({ sub: 'Could not sign in with Joey Wallet.', retry: true });
  }
}

// --- Link another wallet (#447) ----------------------------------------
//
// Prove a SECOND wallet belongs to the same person, so the claim-all card can
// pay out to it. Two arms — a Joey proof (never submitted) or a Xaman SignIn
// QR — both ending at a durable identity edge.

let linkPollTimer = null;
let linkPollGen = 0;

function renderLink({ sub, spinner, buttons, link, qrData, push }) {
  const subEl = el('link-sub');
  if (subEl) subEl.textContent = sub;
  const spin = el('link-spinner');
  if (spin) spin.hidden = !spinner;
  const joey = el('link-joey-btn');
  // The Joey arm only exists where /api/config armed it for this surface;
  // the Xaman arm is always available.
  if (joey) joey.hidden = !buttons || !wcConfig();
  const xaman = el('link-xaman-btn');
  if (xaman) xaman.hidden = !buttons;
  applySignDelivery({
    qrEl: el('link-qr'),
    linkBtn: el('link-link-btn'),
    toggleBtn: el('link-qr-toggle'),
    link, qrData, push,
  });
}

function startLinkWallet() {
  clearTimeout(linkPollTimer);
  linkPollGen++;
  showPanel('link-panel');
  renderLink({
    sub: 'Prove a second wallet is yours — sign in with it, then it can receive your BRIX.',
    buttons: true,
  });
}

function finishLink(s) {
  clearTimeout(linkPollTimer);
  linkPollGen++;
  toast(`🔗 Linked ${s.wallet}`);
  loadBrix();
  showMintHome();
}

async function startLinkJoey() {
  const wc = wcConfig();
  if (!wc) { showError('Joey Wallet is not available here.'); return; }
  const gen = ++linkPollGen;
  const stale = () => gen !== linkPollGen || !!(el('link-panel') || {}).hidden;
  renderLink({ sub: 'Opening Joey Wallet…', spinner: true });
  // The proving wallet's pairing is BORROWED for exactly one signature and
  // released in the finally below — success, decline or crash. It must never
  // become the session pairing: adopting it would repoint every later signTx
  // (and the next reload's restore) at the linked wallet while the LFG
  // session still belongs to the signed-in one.
  let mod = null;
  let borrowedTopic = null;
  try {
    const start = await api('/api/wallet/link', {
      method: 'POST',
      body: JSON.stringify({ provider: 'walletconnect' }),
    });
    mod = await loadWc();
    // fresh: the point is to bring a DIFFERENT wallet than the session's, so
    // never silently reuse the pairing that is already signed in.
    const borrowed = await mod.connect({
      projectId: wc.project_id, chain: wc.chain, metadata: wcMetadata(), fresh: true,
    });
    borrowedTopic = borrowed.topic;
    const wallet = borrowed.wallet;
    if (!wallet) throw new Error('Joey Wallet did not share an XRPL account.');
    if (stale()) return;
    renderLink({ sub: 'Approve the linking request in Joey Wallet…', spinner: true });
    const resp = await mod.signTx({
      chain: wc.chain, txJson: wcProofTx(wallet, start), autofill: false, submit: false,
      topic: borrowedTopic, // sign as the PROVING wallet, not the session's
    });
    const s = await api('/api/wallet/link/proof', {
      method: 'POST',
      body: JSON.stringify({ sign_id: start.sign_id, tx_json: resp.tx_json }),
    });
    if (stale()) return;
    finishLink(s);
  } catch (e) {
    if (stale()) return;
    if (signDeliveryPure.isWcRejection(e)) {
      renderLink({ sub: 'Linking declined in Joey Wallet.', buttons: true });
      return;
    }
    renderLink({ sub: e.message || 'Could not link that wallet.', buttons: true });
  } finally {
    // release() is a no-op on the session's own topic, so this can never tear
    // down the signed-in wallet's pairing.
    if (mod && borrowedTopic) { try { await mod.release(borrowedTopic); } catch (_) { /* gone */ } }
  }
}

async function startLinkXaman() {
  const gen = ++linkPollGen;
  const stale = () => gen !== linkPollGen || !!(el('link-panel') || {}).hidden;
  renderLink({ sub: 'Setting up the Xaman sign-in…', spinner: true });
  let s;
  try {
    s = await api('/api/wallet/link', { method: 'POST', body: JSON.stringify({ provider: 'xaman' }) });
  } catch (e) {
    if (stale()) return;
    renderLink({ sub: e.message || 'Could not start the Xaman sign-in.', buttons: true });
    return;
  }
  if (stale()) return;
  renderLink({
    sub: 'Sign in with the OTHER wallet in Xaman — only approve if both wallets are yours.',
    link: s.signin_link,
    qrData: s.signin_link, // same as the sign-in panel: the deep link IS the QR
  });
  pollLinkXaman(s.uuid, gen);
}

// Transient-failure budget: ~2 minutes of 3 s ticks before we stop pretending
// the request is still alive and hand the user the buttons back.
const LINK_MAX_TRANSIENT = 40;

function pollLinkXaman(uuid, gen) {
  clearTimeout(linkPollTimer);
  const stale = () => gen !== linkPollGen || !!(el('link-panel') || {}).hidden;
  let transient = 0;
  const tick = async () => {
    if (stale()) return;
    let s;
    try {
      s = await api(`/api/wallet/link/${uuid}`);
    } catch (e) {
      if (stale()) return;
      const code = (e.body || {}).code;
      if (code === 'same_wallet') {
        renderLink({ sub: e.message || 'That is the wallet you are already signed in with.', buttons: true });
        return;
      }
      if (e.status === 404) {
        // Pruned server-side (expired, or already consumed) — never surface
        // the bare "not found" to the user.
        renderLink({ sub: 'This link request is no longer valid.', buttons: true });
        return;
      }
      if (++transient > LINK_MAX_TRANSIENT) {
        renderLink({ sub: 'Lost contact with the sign-in request — try again.', buttons: true });
        return;
      }
      linkPollTimer = setTimeout(tick, WC_POLL_MS); // transient; keep polling
      return;
    }
    transient = 0; // a good response re-arms the budget
    if (stale()) return;
    if (s.state === 'linked') { finishLink(s); return; }
    if (s.state === 'expired') {
      renderLink({ sub: 'The sign-in request expired.', buttons: true });
      return;
    }
    if (s.state === 'opened') {
      renderLink({ sub: 'QR scanned — approve the sign-in in Xaman…', spinner: true });
    }
    linkPollTimer = setTimeout(tick, WC_POLL_MS);
  };
  linkPollTimer = setTimeout(tick, WC_POLL_MS);
}

// --- "Share on X" (#41 T9) ---------------------------------------------
//
// Spec x-integration §6.1: the shared `url=` is PUBLIC_SHARE_BASE_URL's own
// OG card page when the operator has configured one, else bithomp's NFT
// page (bithomp already serves its own OG tags, so links still render a
// card either way). Both bases come from /api/config — never derived from
// the page's own browser-reported address (see the shareBase declaration above).

function bithompNftUrl(nftId) {
  return `${bithompBase}/en/nft/${nftId}`;
}

// XRPL classic-address shape (client-side gate only; the service re-validates).
const XRPL_ADDR_RE = /^r[1-9A-HJ-NP-Za-km-z]{24,34}$/;

function shareUrlFor(nftNumber, nftId) {
  if (shareBase && nftNumber != null) {
    // Attribution (#41 follow-on): tag the link with the sharer's wallet so
    // the card page can log whose shares get clicked. Wallets are public
    // on-chain — nothing new is leaked.
    const ref = me && me.wallet && XRPL_ADDR_RE.test(me.wallet)
      ? `?ref=${encodeURIComponent(me.wallet)}`
      : '';
    return `${shareBase}/nft/${nftNumber}${ref}`;
  }
  if (bithompBase && nftId) return bithompNftUrl(nftId);
  // No base is known (every /api/config fetch failed) — return '' so the
  // callers skip/hide the share control instead of rendering a dead
  // relative link.
  return '';
}

function mintShareText(nftNumber) {
  // nft_number can be null/undefined in edge cases (mirrors swapShareText below)
  // — don't render a literal "#null"/"#undefined" in the tweet text.
  return nftNumber != null
    ? `I just minted LFG #${nftNumber}! 🧱 @letseffinggo #XRPL`
    : 'I just minted an LFG! 🧱 @letseffinggo #XRPL';
}

function swapShareText(nftNumber) {
  // nft_number can be null (extract_nft_number found no "#<digits>" in the
  // display name) — the URL still falls back to bithomp via shareUrlFor, but
  // the tweet text can't reference a number that doesn't exist.
  return nftNumber != null
    ? `I just swapped traits on LFG #${nftNumber}! 🧱 @letseffinggo #XRPL`
    : 'I just swapped traits on my LFG! 🧱 @letseffinggo #XRPL';
}

function assembleShareText(nftNumber) {
  // Same null-guard as mintShareText/swapShareText; mirrors the server's
  // _x_share_text("assemble", ...) so both paths tweet identical copy.
  return nftNumber != null
    ? `I just built LFG #${nftNumber}! 🧱 @letseffinggo #XRPL`
    : 'I just built my LFG! 🧱 @letseffinggo #XRPL';
}

function equipShareText(nftNumber) {
  // Mirrors the server's _x_share_text("equip", ...) so the Web Intent and
  // own-account paths tweet identical copy.
  return nftNumber != null
    ? `I just restyled LFG #${nftNumber}! 🧱 @letseffinggo #XRPL`
    : 'I just restyled my LFG! 🧱 @letseffinggo #XRPL';
}

// Build a "Share on X" control: a real <a target=_blank> anchor (Task 0's
// iframe verification of window.open/openExternal inside the sandboxed
// Activity is tracked separately — a genuine anchor href is the fail-safe
// either way, not just a JS-only click handler) plus a "Copy link"
// affordance. Never window.confirm/alert for feedback — both are silent
// no-ops inside the Discord Activity iframe.
function buildShareControl(text, url, meta) {
  const intentUrl = `https://x.com/intent/tweet?text=${encodeURIComponent(text)}&url=${encodeURIComponent(url)}`;

  const wrap = document.createElement('p');
  wrap.className = 'share-row';

  const link = document.createElement('a');
  link.className = 'link';
  link.textContent = '🐦 Share on X';
  link.href = intentUrl;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.onclick = (e) => {
    // Route through the SDK-aware helper first (best chance of breaking out
    // of the sandboxed iframe cleanly); the anchor's real href/target=_blank
    // stays as the fallback for a middle-click, long-press, or a right-click
    // "open in new tab" if the handler doesn't fire.
    e.preventDefault();
    openExternal(intentUrl);
    // Beacon the press itself (exact giveaway eligibility; the card-page
    // Twitterbot fetch is only a proxy). Fire-and-forget, after the open so
    // it can never delay or break the composer.
    if (meta && sessionToken) {
      try {
        api('/api/share/intent', {
          method: 'POST',
          body: JSON.stringify({ kind: meta.kind || 'mint', nft_number: meta.nftNumber ?? null }),
        }).catch(() => {});
      } catch (_) { /* never block the share */ }
    }
  };

  const copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.className = 'link';
  copyBtn.textContent = 'Copy link';

  const copyInput = document.createElement('input');
  copyInput.type = 'text';
  copyInput.className = 'copy-input';
  copyInput.readOnly = true;
  copyInput.hidden = true;
  copyInput.setAttribute('aria-label', 'Share link');
  // "Copy link" hands over the pasteable NFT page/bithomp link (renders a
  // card anywhere it's pasted), NOT the X intent/composer deep-link — that
  // stays exclusive to the "Share on X" anchor/openExternal above.
  copyInput.value = url;

  copyBtn.onclick = async () => {
    try {
      await navigator.clipboard.writeText(url);
      const original = copyBtn.textContent;
      copyBtn.textContent = 'Copied!';
      setTimeout(() => { copyBtn.textContent = original; }, 2000);
    } catch (_) {
      // Clipboard API unavailable/denied: reveal the readonly input so the
      // user can select-all and copy by hand.
      copyInput.hidden = false;
      copyInput.focus();
      copyInput.select();
    }
  };

  wrap.append(link, copyBtn, copyInput);

  // #252: "Share from my account" — server-side post via the user's own
  // connected X account. Only rendered when the service reports the feature
  // armed AND the caller supplied share metadata (kind + nft number; the
  // tweet text is built server-side). Falls back to the Web Intent composer
  // when not connected or on any failure — the intent link above never
  // depends on this path.
  if (xUserShare && meta) {
    const mineBtn = document.createElement('button');
    mineBtn.type = 'button';
    mineBtn.className = 'link';
    mineBtn.textContent = 'Share from my account';
    mineBtn.onclick = async () => {
      mineBtn.disabled = true;
      const original = mineBtn.textContent;
      mineBtn.textContent = 'Sharing…';
      try {
        const res = await api('/api/x/share', {
          method: 'POST',
          body: JSON.stringify({ kind: meta.kind || 'mint', nft_number: meta.nftNumber ?? null }),
        });
        if (res && res.posted) {
          mineBtn.textContent = 'Shared! 🎉';
          return; // stays disabled — one server-side post per control
        }
        // Unexpected 2xx shape: fall back to the zero-OAuth composer.
        openExternal(intentUrl);
        mineBtn.textContent = original;
        mineBtn.disabled = false;
      } catch (e) {
        // api() throws on non-2xx with the body attached (see api()).
        if (e && e.body && e.body.code === 'not_connected') {
          // Kick off the OAuth connect dance in a new window, then let the
          // user click again once connected.
          try {
            const conn = await api('/api/x/connect');
            if (conn && conn.authorize_url) {
              openExternal(conn.authorize_url);
              mineBtn.textContent = 'Connect in the opened window, then retry';
              mineBtn.disabled = false;
              setTimeout(() => { mineBtn.textContent = original; }, 8000);
              return;
            }
          } catch (_) { /* fall through to Web Intent */ }
        }
        openExternal(intentUrl);
        mineBtn.textContent = original;
        mineBtn.disabled = false;
      }
    };
    wrap.appendChild(mineBtn);
  }

  return wrap;
}

async function setupDiscord() {
  // SDK is vendored same-origin (webapp/client/vendor/) to avoid esm.sh's
  // root-absolute re-exports, which break under the Activity's /.proxy sub-path.
  const { DiscordSDK, Common } = await import('./vendor/embedded-app-sdk.js');
  const cfg = await api('/api/config');
  // Second chance for the share bases: main()'s own /api/config fetch
  // swallows failures, and without this repopulation a transient failure
  // there would leave every share link dead for the session.
  applyShareConfig(cfg);
  const clientId = cfg.client_id;
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

const ALL_PANELS = ['register-panel', 'mint-panel', 'flow-panel', 'bulk-panel',
                    'swap-panel', 'swap-traits-panel', 'swap-result-panel',
                    'dressup-panel', 'market-panel', 'market-list-form-panel',
                    'offers-panel', 'trustline-panel', 'claimall-panel',
                    'link-panel'];

function showPanel(id) {
  for (const panel of ALL_PANELS) {
    const hide = panel !== id;
    // Skip panels a cached older index.html doesn't have — a throw here would
    // leave the app unable to render ANY panel (CodeRabbit on #450).
    const panelEl = el(panel);
    if (!panelEl) continue;
    panelEl.hidden = hide;
    // A display:none <video> keeps playing (and decoding) — pause any in the
    // panels being hidden. setMedia re-arms playback on re-entry.
    if (hide) panelEl.querySelectorAll('video').forEach((v) => v.pause());
  }
}

function showMintHome() {
  // Greptile #376 P1: boot resume attaches only ONE flow (single panel), so
  // when a second flow was live too, the next home landing re-checks — the
  // just-finished flow is terminal now (pruned/skipped), so the other one
  // surfaces instead of staying hidden until a full relaunch. One-shot: the
  // flag is cleared before the re-check, and only re-arms if resumeAnyFlow
  // finds yet another concurrent flow — plain navigation can never loop.
  if (resumeRecheckArmed) {
    resumeRecheckArmed = false;
    resumeAnyFlow().then((resumed) => { if (!resumed) showMintHome(); });
    return;
  }
  el('wallet-display').textContent = me.wallet;
  showPanel('mint-panel');
  status(`Hey ${me.username} — welcome to the job site.`);
  loadLeaderboard();
  brixLock = null; // a fresh landing gets a fresh look at claimability
  brixTrustlineNeeded = false;
  loadBrix();
  refreshOffersBadge();
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
    img.src = imgUrl(row.image, THUMB_W);
    img.loading = 'lazy';
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

// --- Daily BRIX drip card (#48, home screen) ---

// Poll cadence for a claim left in a non-terminal state. The payout is a
// single Payment, so this is seconds — but an "unknown" outcome is resolved by
// the server-side recovery job, not by us, so the poll gives up rather than
// hammering the endpoint forever.
const BRIX_POLL_MS = 3000;
const BRIX_POLL_MAX = 20;

let brixPollTimer = null;
let brixPollGen = 0; // bumps on every poll start, invalidating in-flight ticks
let brixLoadGen = 0; // bumps on every loadBrix(), invalidating stale responses
let brixClaiming = false;
// Set from claimErrorView().lockLabel after claims_disabled/trustline_required
// — preconditions GET /api/brix cannot express, so the post-error reload would
// otherwise re-arm the button on the still-positive balance. Pins the button
// disabled until the next home landing clears it. Every other code leaves it
// null: the refreshed status is the truth there. Greptile P1s on PR #434.
let brixLock = null;
// #441: true while brixLock came from trustline_required — the ONE lock that
// is actionable: the button stays enabled under its label and the click runs
// the TrustSet flow instead of another doomed claim POST.
let brixTrustlineNeeded = false;

function stopBrixPoll() {
  brixPollGen++;
  if (brixPollTimer) {
    clearTimeout(brixPollTimer);
    brixPollTimer = null;
  }
}

function renderBrixCard(view) {
  // The drip is one action button in the home row, not a section: same class,
  // size and alignment as Mint/Build/Swap/Trade. The explanatory copy the old
  // card rendered lives on as the button's tooltip/aria label.
  const btn = el('brix-claim-btn');
  btn.hidden = !view.visible;
  if (!view.visible) return;
  btn.title = view.sub;
  btn.setAttribute('aria-label', `${view.button.label} — ${view.sub}`);
  if (brixLock && !view.inFlight) {
    btn.textContent = `\u{1F9F1} ${brixLock}`;
    btn.disabled = !brixTrustlineNeeded || brixClaiming;
    return;
  }
  btn.textContent = `\u{1F9F1} ${view.button.label}`;
  // brixClaiming guards the window between the POST and its response, where
  // the freshly-rendered view still shows the old (claimable) balance.
  btn.disabled = view.button.disabled || brixClaiming;
}

// { poll: false } refreshes the card without (re)starting the claim poll —
// used once pollBrixClaim's budget is spent, so the bound is real and an
// unresolved claim is handed to server-side recovery (Greptile P1, PR #434).
async function loadBrix({ poll = true } = {}) {
  // Every load is pinned to the load + poll generations it was issued under:
  // if either moved while the fetch was in flight, the (possibly stale,
  // still-open) status is dropped rather than repainted over a newer render
  // or re-polled for an obsolete claim (Greptile + CodeRabbit on PR #434).
  const loadGen = ++brixLoadGen;
  const pollGen = brixPollGen;
  let status = null;
  try {
    status = await api('/api/brix');
  } catch (e) {
    // The drip is a bonus tile, never the point of the page: a failed fetch
    // hides it silently rather than throwing an error toast over the home
    // screen the user actually came for.
    status = null;
  }
  if (loadGen !== brixLoadGen || pollGen !== brixPollGen) return null;
  const view = brixPure.brixCardView(status);
  renderBrixCard(view);
  // poll:false never touches the poll — an exhausted refresh resolving late
  // must not stopBrixPoll() a poll a newer home landing has since started.
  if (!poll) return view;
  if (view.visible && view.pollClaimId) pollBrixClaim(view.pollClaimId);
  else stopBrixPoll();
  return view;
}

// Follow one claim to a terminal state. Only the claim's OWNER can read it
// (the endpoint 404s otherwise), so any failure here just stops the poll.
function pollBrixClaim(claimId) {
  stopBrixPoll();
  const gen = brixPollGen;
  let ticks = 0;
  const tick = async () => {
    if (gen !== brixPollGen) return;
    ticks++;
    let res = null;
    try {
      res = await api(`/api/brix/claim/${claimId}`);
    } catch (e) {
      // A transient poll failure (network blip, 5xx) must not strand the
      // button on "Claiming…" until the next home landing — keep ticking
      // within the same budget (Greptile P1 on PR #434). A 404 (claim gone /
      // not ours) keeps failing and simply burns down the same bound.
      if (gen !== brixPollGen) return;
      if (ticks >= BRIX_POLL_MAX) { loadBrix({ poll: false }); return; }
      brixPollTimer = setTimeout(tick, BRIX_POLL_MS);
      return;
    }
    if (gen !== brixPollGen) return;
    if (brixPure.isClaimTerminal(res.state)) {
      if (res.state === 'confirmed') toast(`🧱 ${res.amount} BRIX landed in your wallet.`);
      else toast('That BRIX claim did not go through — your balance is back.');
      loadBrix();
      return;
    }
    // Budget exhausted: server-side recovery owns the claim from here, but
    // re-read status so the card reflects it instead of a frozen button.
    if (ticks >= BRIX_POLL_MAX) { loadBrix({ poll: false }); return; }
    brixPollTimer = setTimeout(tick, BRIX_POLL_MS);
  };
  brixPollTimer = setTimeout(tick, BRIX_POLL_MS);
}

async function claimBrix() {
  if (brixTrustlineNeeded) { startBrixTrustline({ back: showMintHome }); return; }
  const btn = el('brix-claim-btn');
  if (brixClaiming) return;
  // Re-read the balance before asking: the amount goes into the confirm copy,
  // and the accrual cron may have moved it since the card was rendered.
  let fresh = null;
  try {
    fresh = await api('/api/brix');
  } catch (e) {
    toast('Could not read your BRIX balance right now.');
    return; // leave the card exactly as it is — nothing was claimed
  }
  const view = brixPure.brixCardView(fresh);
  renderBrixCard(view);
  if (view.claimable <= 0 || view.inFlight) {
    if (view.pollClaimId) pollBrixClaim(view.pollClaimId);
    return;
  }
  // #446: with more than one linked wallet holding BRIX (or the balance
  // living on a linked wallet, not this one), claim through the fan-out
  // endpoint instead of a solo claim.
  const summary = brixPure.linkedClaimSummary(fresh);
  if (summary.useClaimAll) {
    await startClaimAll(summary);
    return;
  }
  const ok = await confirmDialog({
    title: `Claim ${view.headline}?`,
    text: 'This pays your accrued BRIX out to your wallet on the XRP Ledger.',
    confirmLabel: 'Claim it',
  });
  if (!ok) return;

  // A second POST while the first is in flight would bind nothing and report a
  // false error (409 claim_in_flight), so the button is locked for the round trip.
  brixClaiming = true;
  btn.disabled = true;
  btn.textContent = 'Claiming…';
  try {
    const res = await api('/api/brix/claim', { method: 'POST', body: '{}' });
    if (brixPure.isClaimTerminal(res.state)) {
      if (res.state === 'confirmed') toast(`🧱 ${res.amount} BRIX landed in your wallet.`);
      else toast('That BRIX claim did not go through — your balance is back.');
    } else {
      toast('Claim submitted — settling on the ledger…');
      pollBrixClaim(res.claim_id);
    }
  } catch (e) {
    const code = (e.body && e.body.code) || '';
    // claimErrorView is the single place that decides whether a code may be
    // retried — notably claim_unconfirmed may NOT be, since the server left
    // the accruals bound to a payout that may well have landed (the reload
    // below then sees the open claim and polls it). claims_disabled /
    // trustline_required pin the button (lockLabel) so the reload cannot
    // re-arm it on a balance the server refuses to pay.
    const ev = brixPure.claimErrorView(code);
    toast(ev.message);
    if (ev.lockLabel) brixLock = ev.lockLabel;
    brixTrustlineNeeded = !!ev.trustline;
  } finally {
    brixClaiming = false;
  }
  await loadBrix();
}

function setupBrixCard() {
  el('brix-claim-btn').addEventListener('click', claimBrix);
  // Guarded: a client on a cached pre-#446 index.html has no claimall panel,
  // and a throw here would strip the BRIX button of its click handler too.
  const claimAllBack = el('claimall-back-btn');
  if (claimAllBack) claimAllBack.addEventListener('click', () => {
    claimAllPollGen++; // orphan any in-flight poll
    clearTimeout(claimAllPollTimer);
    loadBrix();
    showMintHome();
  });
  el('trustline-back-btn').addEventListener('click', () => {
    clearTimeout(trustlinePollTimer);
    (trustlineBack || showMintHome)();
  });
  el('trustline-retry-btn').addEventListener('click', () => startBrixTrustline({ back: trustlineBack, onSet: trustlineOnSet }));
}

// --- BRIX claim-all across linked wallets (#446) --------------------------
//
// One POST fans out sequential per-wallet claims server-side; this panel
// polls the job and renders one row per wallet. A wallet skipped for a
// missing trustline gets a per-row "Set trustline" action (the #441 flow,
// aimed at that wallet), then the user claims again.
let claimAllPollTimer = null;
let claimAllPollGen = 0;

function shortWallet(w) {
  return w.length > 12 ? `${w.slice(0, 6)}…${w.slice(-4)}` : w;
}

function renderClaimAll(job) {
  const v = brixPure.claimAllJobView(job);
  // Stale cached index.html without the panel: nothing to paint into (the
  // start path refuses before creating a job in that case; this guard keeps
  // a late poll render from throwing too).
  const sub = el('claimall-sub');
  const box = el('claimall-rows');
  if (!sub || !box) return v;
  sub.textContent = v.sub;
  box.replaceChildren(...v.rows.map((row) => {
    const div = document.createElement('div');
    div.className = 'claimall-row';
    const code = document.createElement('code');
    code.textContent = shortWallet(row.wallet);
    const span = document.createElement('span');
    span.textContent = ` ${row.spinner ? '⏳ ' : ''}${row.text}`;
    div.append(code, span);
    if (row.trustline) {
      const btn = document.createElement('button');
      btn.className = 'link';
      btn.textContent = 'Set trustline';
      btn.addEventListener('click', () => startBrixTrustline({
        wallet: row.wallet,
        back: () => { showPanel('claimall-panel'); },
      }));
      div.append(btn);
    }
    return div;
  }));
  return v;
}

async function startClaimAll(summary) {
  // A cached pre-#446 index.html has no progress panel — refuse BEFORE the
  // POST creates a server job the user could never watch (Greptile on #450).
  if (!el('claimall-panel') || !el('claimall-rows')) {
    toast('Please refresh the app to claim across linked wallets.');
    return;
  }
  const ok = await confirmDialog({
    title: `Claim ${summary.total} BRIX across ${summary.wallets.length} wallets?`,
    text: 'This pays each linked wallet its own accrued BRIX, one payout at a time.',
    confirmLabel: 'Claim it all',
  });
  if (!ok) return;
  let res;
  try {
    res = await api('/api/brix/claim/all', { method: 'POST', body: '{}' });
  } catch (e) {
    const code = (e.body && e.body.code) || '';
    if (code === 'claim_all_in_flight') toast('A claim-all is already running.');
    else if (code === 'bucket_unavailable') toast('Linked wallets could not be resolved — try again in a minute.');
    else toast(brixPure.claimErrorView(code).message);
    return;
  }
  showPanel('claimall-panel');
  renderClaimAll({ state: 'running', wallets: res.wallets });
  pollClaimAllJob(res.job_id);
}

function pollClaimAllJob(jobId) {
  clearTimeout(claimAllPollTimer);
  const gen = ++claimAllPollGen;
  const stale = () => gen !== claimAllPollGen || el('claimall-panel').hidden;
  const tick = async () => {
    if (stale()) return;
    let job;
    try {
      job = await api(`/api/brix/claim/all/${jobId}`);
    } catch (e) {
      if (stale()) return;
      // 404 = the job aged out (or a restart dropped it) — every underlying
      // claim is durable server-side, so just fall back to the card.
      if (e.status === 404) { loadBrix(); showMintHome(); return; }
      claimAllPollTimer = setTimeout(tick, 2000);
      return;
    }
    if (stale()) return;
    const v = renderClaimAll(job);
    if (v.terminal) { loadBrix({ poll: false }); return; }
    claimAllPollTimer = setTimeout(tick, 2000);
  };
  claimAllPollTimer = setTimeout(tick, 2000);
}

// --- BRIX trustline flow (#441) ------------------------------------------
//
// The exit from a 409 trustline_required (Claim, trait Buy). Stateless
// server-side beyond the in-flight payload, so there is nothing to resume:
// on relaunch the user simply retries the original action and either it
// proceeds (line landed) or the 409 re-arms this flow. `back` is where the
// panel returns to on Back / after a signed line.
let trustlinePollTimer = null;
let trustlineBack = null;
// #441: continuation once the line is confirmed set (e.g. re-issue the trait
// buy that 409'd); falls back to trustlineBack.
let trustlineOnSet = null;

function renderTrustline({ sub, spinner, retry, link, qrData, push }) {
  el('trustline-sub').textContent = sub;
  el('trustline-spinner').hidden = !spinner;
  applySignDelivery({
    qrEl: el('trustline-qr'),
    linkBtn: el('trustline-link-btn'),
    toggleBtn: el('trustline-qr-toggle'),
    link, qrData, push,
  });
  el('trustline-retry-btn').hidden = !retry;
}

async function startBrixTrustline({ back, onSet, wallet } = {}) {
  clearTimeout(trustlinePollTimer);
  const gen = ++trustlinePollGen; // orphan any in-flight response from a prior flow
  trustlineBack = back || showMintHome;
  trustlineOnSet = onSet || null;
  showPanel('trustline-panel');
  renderTrustline({ sub: 'Setting up the trustline request…', spinner: true });
  let s;
  try {
    // #446: `wallet` targets a linked wallet from the claim-all card; the
    // server checks bucket membership and pins the TrustSet to that account.
    s = await api('/api/brix/trustline', { method: 'POST', body: JSON.stringify(wallet ? { wallet } : {}) });
  } catch (e) {
    if (gen !== trustlinePollGen || el('trustline-panel').hidden) return;
    renderTrustline({ sub: 'Could not start the trustline request.', retry: true });
    return;
  }
  // The user backed out (or a replacement flow started) while the POST was
  // in flight: this response owns nothing any more.
  if (gen !== trustlinePollGen || el('trustline-panel').hidden) return;
  if (s.state === 'already_set') { finishTrustline(s.state); return; }
  const pending = brixPure.trustlineView('pending');
  renderTrustline({
    ...pending,
    // #447: a LINKED wallet's trustline is always a Xaman QR, even inside a
    // Joey session (the server downgrades it) — name the wallet to scan with.
    sub: wallet ? `Scan with the Xaman app holding ${wallet}` : pending.sub,
    link: s.xumm_url, qrData: s.xumm_url, push: s.push,
  });
  pollTrustline(s.uuid, s);
}

function finishTrustline(state, code) {
  const v = brixPure.trustlineView(state, code);
  renderTrustline(v);
  if (v.clearLock) {
    brixLock = null;
    brixTrustlineNeeded = false;
    toast(`\u{1F9F1} ${v.sub}`);
    (trustlineOnSet || trustlineBack || showMintHome)();
  }
}

let trustlinePollGen = 0;
function pollTrustline(uuid, started) {
  clearTimeout(trustlinePollTimer);
  const gen = ++trustlinePollGen;
  // Stale once the user navigated away or a newer flow superseded this one —
  // checked AFTER the await too, so a late response can never fire the
  // captured onSet continuation (e.g. re-issue an old trait buy) over
  // whatever panel the user is on now.
  const stale = () => gen !== trustlinePollGen || el('trustline-panel').hidden;
  const tick = async () => {
    if (stale()) return;
    let s;
    try {
      s = await api(`/api/brix/trustline/${uuid}`);
    } catch (e) {
      if (stale()) return;
      if (e.status === 404) { finishTrustline('expired'); return; } // record pruned server-side
      trustlinePollTimer = setTimeout(tick, 3000); // transient; keep polling
      return;
    }
    if (stale()) return;
    if (brixPure.isTrustlineTerminal(s.state)) { finishTrustline(s.state, s.code); return; }
    // Re-render keeps the deep link / QR up; applySignDelivery auto-opens
    // at most once per link.
    renderTrustline({
      ...brixPure.trustlineView(s.state),
      link: started.xumm_url, qrData: started.xumm_url, push: started.push,
    });
    trustlinePollTimer = setTimeout(tick, 3000);
  };
  trustlinePollTimer = setTimeout(tick, 3000);
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

// #212: honest sign-request delivery text. `push` comes from the backend per
// payload: 'sent' = the request was push-delivered to the user's Xaman app,
// 'failed' = a push was attempted but XUMM couldn't deliver it (the request
// still appears under Xaman's Events list), null/undefined = plain QR sign
// (no stored push token). The QR/deep link always remain as the fallback.
function signText(push, base) {
  if (push === 'sent') return `${base} We also sent it straight to your Xaman app — just approve it there.`;
  if (push === 'failed') return `${base} (It's also waiting under Events in Xaman.)`;
  return base;
}

function showFlow({ title, text, qrData, link, push, image, video, done, stage, spinner, celebrate, pill, regen, cancel, share, qtyStepper }) {
  flowRenderGen++;
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
  // #142: deep-link-primary on touch devices (auto-open once per payload),
  // QR-primary on desktop — one decision path for every sign screen.
  applySignDelivery({
    qrEl: el('flow-qr'),
    linkBtn: el('flow-link-btn'),
    toggleBtn: el('flow-qr-toggle'),
    link, qrData, push,
  });
  // The minted NFT is the hero: with an image on screen the QR drops to a
  // compact companion size (issue #22). Animated results play as <video>.
  let hero = el('nft-image');
  if (image) hero = setMedia('nft-image', { image, video });
  else if (hero.tagName === 'VIDEO') hero.pause(); // don't loop while hidden
  hero.hidden = !image;
  el('flow-panel').classList.toggle('with-image', !!image);
  hero.classList.toggle('celebrate', !!(image && celebrate));
  el('flow-regen-btn').hidden = !regen;
  // #215: pay-page quantity stepper. Only mint pay views pass qtyStepper, and
  // only when the server flag is on. A fresh render is never stale — clear the
  // dim/pulse a prior qty change may have left on the reused elements.
  const showQty = !!qtyStepper && bulkCfg.enabled;
  el('flow-qty').hidden = !showQty;
  if (showQty) renderFlowQty();
  el('flow-qr').classList.remove('qr-stale');
  el('flow-regen-btn').classList.remove('needs-regen');
  // Back out of an awaiting-signature screen (issue #141): callers pass a
  // callback so each flow decides what "cancel" means for it. Always assign
  // (null when absent) so a later showFlow can't leave a stale handler on
  // the hidden button.
  el('flow-cancel-btn').hidden = !cancel;
  el('flow-cancel-btn').onclick = cancel || null;
  el('flow-done-btn').hidden = !done;
  // Mint-success terminal state only (#41 T9) — callers pass `share` only
  // from the two showFlow() call sites inside pollMint()'s offer_ready
  // branch, never from a failure/timeout/other-flow call site. A missing
  // share.url (shareUrlFor degraded: no base known) hides the row rather
  // than rendering a dead link.
  const shareRow = el('flow-share-row');
  shareRow.replaceChildren();
  const showShare = !!(share && share.url);
  shareRow.hidden = !showShare;
  if (showShare) shareRow.appendChild(buildShareControl(share.text, share.url, share.meta));
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
      // QR already scanned: the payload may already be signed in Xaman, so
      // cancelMint warns before backing out (payment could still land).
      cancel: () => cancelMint(true),
    };
  }
  return {
    title: '💰 Pay to build',
    text: signText(s.payment_push, xrp
      ? `Pay ${s.pay_amount} XRP to mint your avatar — no trustline needed. Scan with Xaman, approve, and hang tight here.`
      : `Pay ${s.pay_amount || 1} LFGO — burned on mint. Scan with Xaman, approve, and hang tight here.`),
    pill,
    qrData: s.payment_link,
    link: s.payment_link,
    push: s.payment_push,
    stage: s.state,
    regen: true,
    qtyStepper: true,
    // Unscanned QR: nothing can be signed yet — cancel without the warning.
    cancel: () => cancelMint(false),
  };
}


function sponsoredMintView(s) {
  const copy = mintPure.sponsoredMintCopy(s);
  if (!copy) return null;
  return { title: copy.title, text: copy.body };
}

function mintStartView(s) {
  return sponsoredMintView(s) || mintPayView(s);
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
  const gen = ++pollGen;
  const tick = async () => {
    if (gen !== pollGen) return; // superseded by a newer poll chain
    if (el('flow-panel').hidden) return; // user navigated away
    let s;
    try {
      s = await api(`/api/mint/${sessionId}`);
    } catch (e) {
      if (gen === pollGen) pollTimer = setTimeout(tick, 3000); // transient; keep polling
      return;
    }
    if (gen !== pollGen) return; // a newer chain started while we awaited

    const sponsored = sponsoredMintView(s);
    if (s.state === 'offer_ready') {
      consumeRef(); // one attribution per click: mint record written (idempotent)
      if (s.accept_signed) {
        showFlow({
          title: `🎉 #${s.nft_number} claimed!`,
          text: 'The transfer is signed — your new avatar is heading to your wallet. Welcome to the job site.',
          image: s.image_url,
          video: s.video_url, // set by the service once #249 lands; undefined today
          done: true,
          stage: s.state,
          celebrate: true,
          share: { text: mintShareText(s.nft_number), url: shareUrlFor(s.nft_number, s.nft_id), meta: { kind: 'mint', nftNumber: s.nft_number } },
        });
        return;
      }
      showFlow({
        title: `🎉 Minted! #${s.nft_number} is yours`,
        text: s.accept_scanned
          ? 'Approve the transfer in Xaman to claim it to your wallet… Normal XRPL network fees and account reserve requirements may still apply.'
          : signText(s.accept_push, 'Scan to accept the transfer and claim it to your wallet. Welcome to the job site. Normal XRPL network fees and account reserve requirements may still apply.'),
        qrData: s.accept_scanned ? null : s.accept_deeplink,
        spinner: s.accept_scanned,
        link: s.accept_deeplink,
        push: s.accept_push,
        image: s.image_url,
        video: s.video_url,
        done: true,
        stage: s.state,
        celebrate: true,
        share: { text: mintShareText(s.nft_number), url: shareUrlFor(s.nft_number, s.nft_id), meta: { kind: 'mint', nftNumber: s.nft_number } },
      });
      pollTimer = setTimeout(tick, 3000); // keep watching for the accept signature
      return;
    }
    if (s.state === 'payment_timeout') {
      if (sponsored) {
        showFlow({ ...sponsored, done: true });
      } else {
        showFlow({ title: '⏰ Payment timed out', text: 'No payment came through in time. Give it another go.', done: true });
      }
      return;
    }
    if (s.state === 'failed') {
      showFlow({ title: '❌ Mint failed', text: s.error || 'Something went wrong.', done: true });
      return;
    }
    if (s.state === 'cancelled') { showMintHome(); return; } // cancelled elsewhere (issue #141)

    if (s.state === 'awaiting_payment') {
      showFlow(sponsored || mintPayView(s));
    } else if (STAGE_TEXT[s.state]) {
      const [title, stageText] = STAGE_TEXT[s.state];
      const text = sponsored ? `No XRP or LFGO payment. ${stageText.replace("Payment's in. ", '')}` : stageText;
      showFlow({ title, text, stage: s.state, spinner: true });
    }
    pollTimer = setTimeout(tick, 3000);
  };
  pollTimer = setTimeout(tick, 3000);
}

let currentMintId = null;

// Bulk mint UI (#215, pay-page revision): server-flagged via /api/config so
// staging can test before prod. Quantity is chosen on the PAY page now, not
// the home screen. Qty 1 = the untouched single-mint path.
let bulkCfg = { enabled: false, max: 1 };
let mintQty = 1;              // selected quantity on the pay-page stepper
let liveQty = null;           // quantity the live session/job was built for; null = none

function renderFlowQty() {
  el('flow-qty-value').textContent = String(mintQty);
  el('flow-qty-minus').disabled = mintQty <= 1;
  el('flow-qty-plus').disabled = mintQty >= bulkCfg.max;
}

function setupBulkStepper(cfg) {
  bulkCfg = { enabled: !!cfg.bulk_mint_ui, max: Math.max(1, cfg.bulk_mint_max || 1) };
  if (!bulkCfg.enabled) return; // flag off: stepper never renders, today's UI
  // Surface any swallowed handler exception as a toast: a stale-cached
  // module (the 2026-07-21 dead-stepper bug) otherwise fails silently.
  const tap = (delta) => {
    try { onQtyChange(delta); } catch (e) { showError(`qty: ${e.message}`); }
  };
  el('flow-qty-minus').onclick = () => tap(-1);
  el('flow-qty-plus').onclick = () => tap(1);
}

// Pay-page stepper press. Changing quantity invalidates the shown QR: cancel
// the live payload immediately (frees the XUMM slot), dim the QR, and pulse
// Regenerate — a new QR is built only when the user taps it.
function onQtyChange(delta) {
  const next = mintPure.clampQty(mintQty + delta, bulkCfg.max);
  if (next === mintQty) return;
  mintQty = next;
  renderFlowQty();
  if (mintPure.qtyStale(mintQty, liveQty)) {
    cancelLiveMintSilently(); // fire-and-forget: cancel whatever is live
    el('flow-qr').classList.add('qr-stale');
    el('flow-qr').hidden = false;                    // reveal even if collapsed (#142)
    el('flow-qr-toggle').hidden = true;
    el('flow-link-btn').hidden = true;               // no accept while stale
    el('flow-regen-btn').hidden = false;
    el('flow-regen-btn').classList.add('needs-regen');
  }
}

// Cancel whichever mint payload is live without navigating home (used when a
// qty change supersedes it). Stops both poll chains and clears liveQty.
async function cancelLiveMintSilently() {
  const singleId = currentMintId;
  const bulkId = currentBulkId;
  currentMintId = null;
  currentBulkId = null;
  liveQty = null;
  clearTimeout(pollTimer); ++pollGen;             // stop single-mint poll
  clearTimeout(bulkPollTimer); ++bulkPollGen;     // stop bulk poll
  if (singleId) {
    try {
      await api(`/api/mint/${singleId}/cancel`, {
        method: 'POST', body: JSON.stringify(discordCtx()),
      });
    } catch (_) { /* 409 already-paid etc.: superseded anyway, ignore */ }
  }
  if (bulkId) {
    try {
      await api(`/api/mint/bulk/${bulkId}/cancel`, {
        method: 'POST', body: JSON.stringify(discordCtx()),
      });
    } catch (_) { /* ignore */ }
  }
}

// Regenerate = the commit gate. Same quantity + a live single session that
// merely expired -> refresh that session's payload (keeps its state). Any qty
// change (liveQty null) -> build a fresh session on the endpoint the selected
// quantity targets.
async function onFlowRegen() {
  if (!mintPure.qtyStale(mintQty, liveQty) && liveQty === 1 && currentMintId) {
    return regeneratePaymentQr(); // classic same-session expired-QR refresh, has its own disable guard
  }
  // Any other regenerate builds a fresh session; disable the button for the
  // whole cancel+start round trip so a double-tap can never race a second
  // await cancelLiveMintSilently() and launch a second concurrent bulk job,
  // orphaning its XUMM payload + headroom reservation (#226).
  const btn = el('flow-regen-btn');
  btn.disabled = true;
  try {
    await cancelLiveMintSilently();
    if (mintPure.qtyMintTarget(mintQty) === 'bulk') return await startBulkMint(mintQty);
    return await startMint();
  } finally {
    btn.disabled = false;
  }
}

// ---- Bulk mint flow (#215 UI) ----
let currentBulkId = null;
let bulkPollTimer = null;
let bulkPollGen = 0;

function bulkPayView(j) {
  const xrp = j.pay_with === 'XRP';
  return {
    title: `💰 Pay for ${j.quantity} builds`,
    text: j.payment_link
      ? (xrp
        ? `Pay ${j.pay_amount} XRP to mint ${j.quantity} avatars — no trustline needed. Scan with Xaman, approve, and hang tight here.`
        : `Pay ${j.pay_amount} LFGO — burned on mint. One payment covers all ${j.quantity}. Scan with Xaman, approve, and hang tight here.`)
      : 'Preparing your payment request…',
    pill: j.pay_with ? { kind: xrp ? 'xrp' : 'lfgo', text: `Paying with ${xrp ? 'XRP' : 'LFGO'}` } : null,
    qrData: j.payment_link,
    link: j.payment_link,
    push: j.payment_push,
    // No same-session refresh for a fresh bulk job (onFlowRegen always cancels
    // + restarts it) — hide Regenerate; onQtyChange reveals it when a qty
    // change actually invalidates the shown QR.
    qtyStepper: true,
    spinner: !j.payment_link, // payment_link may be null = still preparing (see to_dict contract)
    cancel: () => cancelBulkMint(),
  };
}

async function startBulkMint(quantity) {
  try {
    const j = await api('/api/mint/bulk', {
      method: 'POST',
      body: JSON.stringify({ ...discordCtx(), quantity, ref: stashedRef() }),
    });
    currentBulkId = j.id;
    mintQty = quantity;
    liveQty = quantity;
    showFlow(bulkPayView(j));
    pollBulk(j.id);
  } catch (e) {
    showError(e.message === 'collection_full'
      ? 'The collection is full — no room left to mint.' : e.message);
  }
}

async function cancelBulkMint() {
  if (!currentBulkId) { showMintHome(); return; }
  const btn = el('flow-cancel-btn');
  btn.disabled = true;
  try {
    await api(`/api/mint/bulk/${currentBulkId}/cancel`, {
      method: 'POST', body: JSON.stringify(discordCtx()),
    });
    clearTimeout(bulkPollTimer);
    bulkPollGen++;
    currentBulkId = null;
    showMintHome();
  } catch (e) {
    // 409 = already paid: fulfillment must run — keep polling, don't dump home.
  } finally {
    btn.disabled = false;
  }
}

function unitRow(j, u) {
  const row = document.createElement('div');
  row.className = `bulk-unit ${u.state}`;
  const label = document.createElement('span');
  label.className = 'u-label';
  if (u.state === 'pending') label.textContent = `#${u.index + 1} — waiting…`;
  else if (u.state === 'minted') label.textContent = `#${u.nft_number ?? u.index + 1} — creating offer…`;
  else if (u.state === 'failed') {
    label.innerHTML = '';
    label.textContent = `#${u.index + 1} — didn't mint. `;
    const err = document.createElement('span');
    err.className = 'u-error';
    err.textContent = 'Your payment is saved as a mint credit — nothing is lost.';
    label.appendChild(err);
  } else label.textContent = `#${u.nft_number}`;
  if (u.image_url) {
    const img = document.createElement('img');
    img.className = 'thumb';
    // Through the same-origin proxy like every other CDN image — the raw
    // CDN URL is CSP-blocked inside the Discord Activity.
    img.src = imgUrl(u.image_url, THUMB_W);
    img.alt = `NFT #${u.nft_number}`;
    row.appendChild(img);
  }
  row.appendChild(label);
  if (u.state === 'offered' && u.offer_id) {
    const btn = document.createElement('button');
    btn.className = 'secondary';
    btn.textContent = 'Accept';
    btn.onclick = () => bulkAccept(j.id, u.index, row, btn);
    row.appendChild(btn);
  } else if (u.state === 'offered' && !u.offer_id) {
    const done = document.createElement('span');
    done.textContent = '✅ claimed';
    row.appendChild(done);
  }
  return row;
}

// Accept payloads are built ON CLICK only (XUMM open-payload cap, #260) —
// never pre-created for the whole list.
async function bulkAccept(jobId, index, row, btn) {
  btn.disabled = true;
  try {
    const r = await api(`/api/mint/bulk/${jobId}/units/${index}/accept`, {
      method: 'POST', body: JSON.stringify(discordCtx()),
    });
    let qrWrap = row.querySelector('.u-accept');
    if (!qrWrap) {
      qrWrap = document.createElement('div');
      qrWrap.className = 'u-accept';
      row.appendChild(qrWrap);
    }
    qrWrap.replaceChildren();
    const note = document.createElement('p');
    note.className = 'card-sub';
    note.textContent = signText(r.push, 'Scan to claim this one to your wallet.');
    qrWrap.appendChild(note);
    const img = document.createElement('img');
    img.className = 'u-qr';
    img.alt = 'Accept QR — scan with Xaman';
    qrWrap.appendChild(img);
    const open = document.createElement('button');
    open.className = 'link';
    open.textContent = 'Open in Xaman ↗';
    qrWrap.appendChild(open);
    const toggle = makeQrToggle();
    qrWrap.insertBefore(toggle, img);
    // #142: user tapped "claim" — a fresh sign ask, so auto-open applies.
    applySignDelivery({ qrEl: img, linkBtn: open, toggleBtn: toggle,
      link: r.link, qrData: r.link, push: r.push });
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false; // repeat click = fresh payload (old one expires in 15 min)
  }
}

// ---- Pending-offers tray (#218) ----
// Ledger-driven claim-later surface: every outstanding gift offer locked to
// the caller's wallet, durable across relaunches/restarts (unlike the #215
// bulk accept list, which dies with its in-memory job). Offers never expire.

let offersFetchedAt = 0; // throttle the home-screen badge refresh
const OFFERS_BADGE_TTL_MS = 30000;

async function fetchPendingOffers() {
  const r = await api('/api/offers/pending');
  return r.offers || [];
}

// Badge on the home screen: silent on any failure (the tray is additive —
// a flaky lookup must never toast over the mint home).
async function refreshOffersBadge(force) {
  if (!force && Date.now() - offersFetchedAt < OFFERS_BADGE_TTL_MS) return;
  offersFetchedAt = Date.now();
  let offers = [];
  try { offers = await fetchPendingOffers(); } catch (_) { return; }
  const btn = el('offers-btn');
  btn.hidden = offers.length === 0;
  if (offers.length) btn.textContent = `🎁 Pending offers (${offers.length})`;
}

function offerRow(o) {
  const row = document.createElement('div');
  row.className = 'bulk-unit offered'; // same row styling as the bulk list
  // Trait tokens (Extract) carry their /api/layer art as a same-origin
  // image_url + slot/value; characters carry a CDN image + nft_number. Both
  // fall back to the short nft_id only when neither resolved on-chain.
  const isTrait = o.kind === 'trait';
  const isCloset = o.kind === 'closet';
  const src = isTrait ? traitLayerSrc(o.image_url) : (o.image ? imgUrl(o.image, THUMB_W) : null);
  if (src) {
    const img = document.createElement('img');
    img.className = 'thumb';
    img.src = src;
    img.alt = isTrait ? `${o.slot}: ${o.value}` : (o.nft_number != null ? `NFT #${o.nft_number}` : 'NFT');
    row.appendChild(img);
  } else if (isCloset) {
    // Soulbound Closet has no art tile; a glyph beats a raw nft_id.
    const glyph = document.createElement('span');
    glyph.className = 'thumb';
    glyph.textContent = '🧥';
    glyph.setAttribute('aria-hidden', 'true');
    row.appendChild(glyph);
  }
  const label = document.createElement('span');
  label.className = 'u-label';
  if (isTrait) label.textContent = `${o.slot} — ${o.value}`;
  else if (isCloset) {
    label.textContent = 'Your LFG Closet';
    label.title = 'Free soulbound token from LFG that holds your harvested traits. Accept to unlock Harvest / Build.';
  } else if (o.nft_number != null) label.textContent = `#${o.nft_number}`;
  else label.textContent = `${o.nft_id.slice(0, 8)}…`;
  row.appendChild(label);
  const btn = document.createElement('button');
  btn.className = 'secondary';
  btn.textContent = 'Accept';
  btn.onclick = () => offerAccept(o, row, btn);
  row.appendChild(btn);
  return row;
}

async function openOffers() {
  showPanel('offers-panel');
  const list = el('offers-list');
  list.replaceChildren();
  let offers = [];
  try {
    offers = await fetchPendingOffers();
  } catch (e) {
    showError(e.message);
    return;
  }
  if (!offers.length) {
    const p = document.createElement('p');
    p.className = 'card-sub';
    p.textContent = 'Nothing pending — everything is claimed. 🎉';
    list.appendChild(p);
    return;
  }
  list.replaceChildren(...offers.map(offerRow));
}

// Accept payload built ON CLICK only (open-payload cap, #260), rendered
// inline in the row exactly like the bulk accept QR.
async function offerAccept(o, row, btn) {
  btn.disabled = true;
  try {
    const r = await api('/api/offers/accept', {
      method: 'POST',
      body: JSON.stringify({ ...discordCtx(), offer_index: o.offer_index }),
    });
    let qrWrap = row.querySelector('.u-accept');
    if (!qrWrap) {
      qrWrap = document.createElement('div');
      qrWrap.className = 'u-accept';
      row.appendChild(qrWrap);
    }
    qrWrap.replaceChildren();
    const note = document.createElement('p');
    note.className = 'card-sub';
    note.textContent = signText(r.push, 'Scan to claim this one to your wallet.');
    qrWrap.appendChild(note);
    const img = document.createElement('img');
    img.className = 'u-qr';
    img.alt = 'Accept QR — scan with Xaman';
    qrWrap.appendChild(img);
    const open = document.createElement('button');
    open.className = 'link';
    open.textContent = 'Open in Xaman ↗';
    qrWrap.appendChild(open);
    const toggle = makeQrToggle();
    qrWrap.insertBefore(toggle, img);
    // #142: user tapped "claim" — a fresh sign ask, so auto-open applies.
    applySignDelivery({ qrEl: img, linkBtn: open, toggleBtn: toggle,
      link: r.link, qrData: r.link, push: r.push });
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false; // repeat click = fresh payload, same as bulkAccept
  }
}

function renderBulkJob(j) {
  showPanel('bulk-panel');
  const total = j.quantity;
  if (j.state === 'done') {
    el('bulk-progress').textContent = j.offered === total
      ? `All ${total} minted — accept your NFTs below. Offers never expire.`
      : `Finished: ${j.offered}/${total} ready to accept below.`;
  } else if (j.state === 'failed') {
    el('bulk-progress').textContent = j.error || 'Something went wrong.';
  } else {
    el('bulk-progress').textContent = `Minting ${Math.min(j.minted + 1, total)} / ${total}…`;
  }
  el('bulk-spinner').hidden = j.state === 'done' || j.state === 'failed';
  el('bulk-done-btn').hidden = !(j.state === 'done' || j.state === 'failed');
  const list = el('bulk-units');
  // Preserve any open accept QR across re-renders: only rebuild rows whose
  // state changed. Keyed by unit index on the row element.
  const prev = new Map([...list.children].map((n) => [n.dataset.idx, n]));
  list.replaceChildren(...j.units.map((u) => {
    const old = prev.get(String(u.index));
    // offer_id is part of the reuse key: an offered→claimed transition keeps
    // state 'offered' but nulls offer_id — the row must rebuild to swap the
    // Accept button for the claimed marker.
    if (old && old.dataset.state === u.state
        && old.dataset.offerId === String(u.offer_id)) return old;
    const row = unitRow(j, u);
    row.dataset.idx = String(u.index);
    row.dataset.state = u.state;
    row.dataset.offerId = String(u.offer_id);
    return row;
  }));
}

function pollBulk(jobId) {
  clearTimeout(bulkPollTimer);
  const gen = ++bulkPollGen;
  const tick = async () => {
    if (gen !== bulkPollGen) return;
    let j;
    try {
      j = await api(`/api/mint/bulk/${jobId}`);
    } catch (e) {
      if (gen === bulkPollGen) bulkPollTimer = setTimeout(tick, 3000);
      return;
    }
    if (gen !== bulkPollGen) return;
    if (j.state === 'awaiting_payment') {
      showFlow(bulkPayView(j));
    } else if (j.state === 'payment_timeout') {
      showFlow({ title: '⏰ Payment timed out', text: 'No payment came through in time. Give it another go.', done: true });
      return;
    } else if (j.state === 'cancelled') {
      showMintHome();
      return;
    } else {
      if ((j.minted | 0) > 0) consumeRef(); // one attribution per click: mint record written (idempotent)
      renderBulkJob(j); // paid / fulfilling / done / failed
      if (j.state === 'done' || j.state === 'failed') return; // final render, stop polling
    }
    bulkPollTimer = setTimeout(tick, 3000);
  };
  bulkPollTimer = setTimeout(tick, 1000);
}

// Boot resume (#216 pattern): a live bulk job survives the Activity webview
// being killed while the user app-switches to Xaman. Checked BEFORE the
// single-mint resume — a user can't have both, and bulk is the costlier
// flow to strand. Returns true when a job resumed.
// Re-attach to an already-fetched live bulk job (#221: the consolidated
// /api/sessions/active boot path hands the session in; no refetch).
function attachBulkResume(j) {
  currentBulkId = j.id;
  mintQty = j.quantity;
  liveQty = j.quantity;
  if (j.state === 'awaiting_payment') showFlow(bulkPayView(j));
  else renderBulkJob(j);
  pollBulk(j.id);
  return true;
}

async function startMint() {
  try {
    const s = await api('/api/mint', {
      method: 'POST',
      body: JSON.stringify({ ...discordCtx(), ref: stashedRef() }),
    });
    currentMintId = s.id;
    mintQty = 1;
    liveQty = 1;
    showFlow(mintStartView(s));
    pollMint(s.id);
  } catch (e) {
    showError(e.message);
  }
}

// Mint session resume: Discord mobile kills/reloads the Activity webview when
// the user app-switches to Xaman to sign the payment, losing currentMintId
// while the server-side session keeps running — the user lands back on the
// home screen mid-mint. Called on boot with the already-fetched live session
// (#221: one /api/sessions/active round-trip); re-attach and let the poll
// render its real state.
function attachMintResume(session) {
  const id = mintPure.activeMintSessionId({ session });
  if (!id) return false;
  currentMintId = id;
  mintQty = 1;
  liveQty = 1;
  showFlow(sponsoredMintView(session) || {
    title: '🔄 Reconnecting…',
    text: 'You have a mint in progress — picking it back up where you left off.',
    spinner: true,
    stage: session.state,
    // Warn before backing out only if the QR was already opened in Xaman
    // (same distinction mintPayView draws) — an unscanned payload provably
    // has nothing signed.
    cancel: () => cancelMint(!!session.qr_scanned),
  });
  pollMint(id);
  return true;
}

// Greptile #376 P1: one-shot flag — a second live flow existed at resume
// time; the next showMintHome landing re-runs resumeAnyFlow to surface it.
let resumeRecheckArmed = false;

// Re-attach to a running trait swap: reveal the swap progress panel and let
// pollSwap render whatever the real state is (fee QR, progress, results).
function attachSwapResume(session) {
  showPanel('swap-result-panel');
  el('swap-results').innerHTML = '';
  el('swap-done-btn').hidden = true;
  el('swap-result-title').textContent = '🔄 Reconnecting…';
  el('swap-result-text').textContent =
    'You have a trait swap in progress — picking it back up where you left off.';
  pollSwap(session.id);
  return true;
}

// Greptile #376: kill every poller that renders into the shared flow-panel
// before attaching a resumed flow. pollMint keeps a 3 s watch alive through
// offer_ready (waiting for the accept signature) and its only stop guard is
// flow-panel visibility — a chained market/economy/shop resume keeps that
// panel visible, so the stale mint tick would repaint its result over the
// newly attached flow. Generation bumps invalidate ticks already awaiting
// their fetch; clearTimeout alone cannot.
function invalidateFlowPolls() {
  clearTimeout(pollTimer);
  pollGen++;
  clearTimeout(bulkPollTimer);
  bulkPollGen++;
  clearTimeout(swapPollTimer);
  swapPollGen++;
  clearTimeout(marketFlowTimer);
  marketFlowGen++;
  clearTimeout(shopFlowTimer);
  shopFlowGen++;
  flowRenderGen++;
}

// One boot round-trip (#221): GET /api/sessions/active, pick the
// highest-priority live flow (resume_pure.js), and route it to the existing
// per-flow poller/renderer. Returns true when a flow resumed. Read-only
// re-attach: nothing is signed or started here.
async function resumeAnyFlow() {
  let sessions = null;
  try {
    sessions = await api('/api/sessions/active');
  } catch (_) { return false; /* endpoint unreachable: boot home as before */ }
  const picked = resumePure.pickActiveFlow(sessions);
  if (!picked) return false;
  const { flow, session } = picked;
  invalidateFlowPolls();
  // A second live flow can't be rendered alongside the winner — arm the
  // one-shot home-landing re-check (see showMintHome) so it surfaces once
  // this one finishes.
  resumeRecheckArmed = resumePure.hasOtherActiveFlow(sessions, flow);
  switch (flow) {
    case 'mint': return attachMintResume(session);
    case 'bulk': return attachBulkResume(session);
    case 'swap': return attachSwapResume(session);
    case 'market': return attachMarketResume(session);
    case 'economy': return attachEconomyResume(session);
    case 'shop': await resumeShopBuy(session.id); return true;
  }
  return false;
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

// Back out of the pay screen (issue #141): tell the server to cancel the
// session (releasing the per-user mint lock immediately), then return to the
// mint start screen. If the server refuses — above all 409 'session is past
// payment', meaning the money is already taken — the user must NOT be dumped
// home: keep the session id and resume polling so the flow panel follows the
// real pipeline through to the offer_ready accept QR (or the real failure).
// `maybeSigned` is set by the QR-scanned variant, where the payload may
// already be approved in Xaman: warn before backing out.
async function cancelMint(maybeSigned) {
  if (!currentMintId) { showMintHome(); return; }
  if (maybeSigned) {
    const ok = await confirmDialog({
      title: 'Cancel this mint?',
      text: 'If you already approved the payment in Xaman, it may still go through. Cancel anyway?',
      confirmLabel: 'Cancel mint',
    });
    if (!ok) return;
  }
  const btn = el('flow-cancel-btn');
  btn.disabled = true;
  let cancelResult = null;
  let refetchResult = null;
  try {
    cancelResult = await api(`/api/mint/${currentMintId}/cancel`, {
      method: 'POST', body: JSON.stringify(discordCtx()),
    });
  } catch (e) {
    // Cancel refused (paid session) or failed transiently — look at the
    // real session state before deciding anything.
    try { refetchResult = await api(`/api/mint/${currentMintId}`); } catch (e2) { /* gone */ }
  } finally {
    btn.disabled = false;
  }
  if (mintPure.cancelMintOutcome(cancelResult, refetchResult) === 'resume') {
    // Session still live (or ended some other way): stay on the flow panel
    // and let the poll render the truth — never abandon a paid mint.
    pollMint(currentMintId);
    return;
  }
  clearTimeout(pollTimer);
  currentMintId = null;
  showMintHome();
}

// --- Registration via Xaman Sign In (issue #24) ---

let signinPollTimer = null;

function renderSignin({ sub, spinner, qrLink, retry }) {
  el('register-sub').textContent = sub;
  el('register-spinner').hidden = !spinner;
  // #142: same delivery decision as every other sign screen — the sign-in QR
  // data IS the deep link.
  applySignDelivery({
    qrEl: el('register-qr'),
    linkBtn: el('register-link-btn'),
    toggleBtn: el('register-qr-toggle'),
    link: qrLink, qrData: qrLink,
  });
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

// --- Standalone web surface sign-in (spec 2026-07-16) ---
// Same register-panel QR UI, but the sign-in IS the auth: on approval the
// service returns a platform="web" session token (wallet = identity), which
// persists in localStorage so a reload within the token TTL skips the QR.

async function startWebSignin() {
  clearTimeout(signinPollTimer);
  showPanel('register-panel');
  renderSignin({ sub: 'Setting up your Xaman sign-in…', spinner: true });
  try {
    const s = await api('/api/web/signin', { method: 'POST', body: '{}' });
    renderSignin({
      sub: 'Scan with Xaman and approve the sign-in — your wallet is your login.',
      qrLink: s.signin_link,
    });
    pollWebSignin(s.uuid);
  } catch (e) {
    showError(e.message);
    renderSignin({ sub: 'Could not start the Xaman sign-in.', retry: true });
  }
}

function pollWebSignin(uuid) {
  clearTimeout(signinPollTimer);
  const tick = async () => {
    if (el('register-panel').hidden) return; // user navigated away
    let s;
    try {
      s = await api(`/api/web/signin/${uuid}`);
    } catch (e) {
      signinPollTimer = setTimeout(tick, 3000); // transient; keep polling
      return;
    }
    if (s.state === 'signed') {
      sessionToken = s.session_token;
      try { localStorage.setItem(WEB_SESSION_KEY, s.session_token); } catch (_) { /* private mode */ }
      me = { ...s.user, wallet: s.wallet };
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

// The `provider` claim of a session token, read WITHOUT verifying the
// signature — the server is the only thing that trusts this value; the client
// uses it purely to decide whether to re-attach a Joey pairing (#447).
function sessionProvider(token) {
  try {
    const body = token.split('.')[0].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(body)).provider || null;
  } catch (_) {
    return null;
  }
}

// Re-attach the stored WalletConnect session. Resolves to the wallet, or null
// when there is no live pairing left (wallet-side disconnect, cleared storage).
async function wcRestore() {
  const wc = wcConfig();
  if (!wc) return null;
  try {
    const mod = await loadWc();
    return await mod.restore({ projectId: wc.project_id, metadata: wcMetadata() });
  } catch (e) {
    console.error(e);
    return null;
  }
}

async function setupWeb() {
  let stored = null;
  try { stored = localStorage.getItem(WEB_SESSION_KEY); } catch (_) { /* private mode */ }
  if (stored) {
    sessionToken = stored;
    // #447: a session minted through Joey needs its WalletConnect pairing
    // back before any sign request can be delivered. Only a REAL miss (the
    // pairing is gone) invalidates the session — a transient /api/config
    // failure leaves wcConfig() null, and dropping a good token over that
    // would sign the user out for a blip. In that case keep the token and
    // skip the re-attach; wcSign() re-attaches lazily via !activeWallet().
    if (sessionProvider(stored) === 'walletconnect' && wcConfig() && !(await wcRestore())) {
      sessionToken = null;
      try { localStorage.removeItem(WEB_SESSION_KEY); } catch (_) { /* private mode */ }
      await startWebSignin();
      return null;
    }
    try {
      return await api('/api/me'); // still valid → straight in
    } catch (e) {
      // Only a 401 means the token is dead (api() already dropped the key).
      // Transient network/5xx errors keep the good token and surface as a
      // connect failure instead of forcing a spurious re-sign-in.
      if (e.status !== 401) throw e;
      sessionToken = null;
    }
  }
  await startWebSignin();
  return null; // the sign-in flow drives the UI from here
}

// --- Trait Swapper ---

let swapNfts = [];
let swapPick = [];
let swapCards = []; // {nft, card} for every grid tile, for re-rendering picks
let swapPollTimer = null;
// Poll-chain generation token (same guard as pollMint): a refused cancel
// resumes pollSwap while an old tick may still be awaiting the API — the
// stale tick must not schedule a second chain for the same session.
let swapPollGen = 0;
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
      img.src = imgUrl(nft.image, THUMB_W);
      img.loading = 'lazy';
      img.alt = '';
      const name = document.createElement('span');
      name.className = 'cap';
      name.textContent = nft.name;
      const body = document.createElement('span');
      body.className = 'body';
      body.textContent = nft.gender; // male / female / skeleton / ape
      name.appendChild(body);
      card.replaceChildren(pick, img, name);
      if (nft.video) {
        // Grid tiles stay lightweight stills; the badge flags art that plays
        // as video on the chooser/result screens (#250).
        const anim = document.createElement('span');
        anim.className = 'anim-badge';
        anim.textContent = '▶';
        anim.setAttribute('aria-hidden', 'true');
        card.appendChild(anim);
      }
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
  setMedia('swap-img1', { image: a.image, video: a.video, thumbW: THUMB_W });
  setMedia('swap-img2', { image: b.image, video: b.video, thumbW: THUMB_W });
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
// Keyed on session id AND payment_link: a regenerated QR keeps the id but
// swaps the link, and must re-render or the fresh QR never appears.
let swapPaymentShown = null;
function renderSwapPayment(s) {
  const key = `${s.id}:${s.payment_link}`;
  if (swapPaymentShown === key) return; // already on screen; don't rebuild
  swapPaymentShown = key;
  el('swap-result-title').textContent = '💰 Swap fee required';
  const feeLine = `Pay ${s.fee_amount} ${s.pay_with || 'BRIX'} to swap your NFT(s) in place. `;
  // #447: a WalletConnect request has no QR and no link to open — the sign
  // ask lands in Joey instead, so the Xaman instructions would be nonsense.
  el('swap-result-text').textContent = signDeliveryPure.isWcLink(s.payment_link)
    ? `${feeLine}Approve the request in Joey Wallet.`
    : signText(s.payment_push,
      `${feeLine}Scan the QR with Xaman/XUMM or open the link, approve, then wait here.`);
  const box = el('swap-results');
  const qrImg = document.createElement('img');
  qrImg.className = 'result-qr';
  qrImg.alt = 'QR';
  const btn = document.createElement('button');
  btn.className = 'link';
  btn.textContent = 'Open in Xaman';
  const qrToggle = makeQrToggle();
  // #142: fee payment is a fresh sign ask — deep-link primary + auto-open on
  // touch, QR primary on desktop. (The dedup key already includes the link,
  // so a regenerated QR auto-opens its new payload exactly once.)
  applySignDelivery({ qrEl: qrImg, linkBtn: btn, toggleBtn: qrToggle,
    link: s.payment_link, qrData: s.payment_link, push: s.payment_push });
  // A XUMM payload expires after a few minutes: offer a fresh QR and a way
  // out (mirror of the mint pay screen's regen + cancel — previously a stale
  // fee QR left no exit but closing the whole Activity).
  const regenBtn = document.createElement('button');
  regenBtn.className = 'link';
  regenBtn.textContent = '🔄 QR expired? Get a new one';
  regenBtn.onclick = () => regenerateSwapQr(s.id, regenBtn);
  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'link';
  cancelBtn.textContent = 'Cancel swap';
  cancelBtn.onclick = () => cancelSwap(s.id, cancelBtn);
  box.replaceChildren(qrToggle, qrImg, btn, regenBtn, cancelBtn);
}

async function regenerateSwapQr(sessionId, btn) {
  btn.disabled = true;
  try {
    const s = await api(`/api/swap/${sessionId}/regenerate`, {
      method: 'POST', body: JSON.stringify(discordCtx()),
    });
    if (s.payment_link) renderSwapPayment(s);
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
  }
}

// Back out of the swap fee screen. If the server refuses — above all 409
// 'session is past payment', meaning the fee is already taken — the user
// must NOT be dumped out: keep polling so the panel follows the real
// pipeline through to the results (same decision logic as the mint cancel).
async function cancelSwap(sessionId, btn) {
  const ok = await confirmDialog({
    title: 'Cancel this swap?',
    text: 'If you already approved the fee in Xaman, it may still go through. Cancel anyway?',
    confirmLabel: 'Cancel swap',
  });
  if (!ok) return;
  // Ownership (#376): if a NEW swap/poll chain starts while this cancel's
  // requests are in flight, this stale cancel must become a no-op — it may
  // neither navigate away from the new swap nor consume an armed resume
  // re-check that belongs to the new chain.
  const gen = swapPollGen;
  btn.disabled = true;
  let cancelResult = null;
  let refetchResult = null;
  try {
    cancelResult = await api(`/api/swap/${sessionId}/cancel`, {
      method: 'POST', body: JSON.stringify(discordCtx()),
    });
  } catch (e) {
    try { refetchResult = await api(`/api/swap/${sessionId}`); } catch (e2) { /* gone */ }
  } finally {
    btn.disabled = false;
  }
  if (gen !== swapPollGen) return; // superseded while we awaited
  if (mintPure.cancelMintOutcome(cancelResult, refetchResult) === 'resume') {
    pollSwap(sessionId);
    return;
  }
  clearTimeout(swapPollTimer);
  // A tick already awaiting the status API survives clearTimeout — bump the
  // generation so it can't repaint the fee screen after we leave.
  ++swapPollGen;
  exitSwapAfterCancel();
}

// Leaving a cancelled swap (#376 review): when the boot resume armed the
// chained re-check (a second flow was live), route through showMintHome so it
// is consumed and the other flow surfaces — otherwise the swap picker as
// before.
let swapExitHandled = false; // reset by each pollSwap chain; dedupes the exits
function exitSwapAfterCancel() {
  // The cancel handler and pollSwap's cancelled-elsewhere branch can race —
  // both observing the same cancellation. Only the FIRST exit acts: a second
  // call would find the re-check flag already consumed and open the swap
  // picker over the freshly resumed flow.
  if (swapExitHandled) return;
  swapExitHandled = true;
  if (resumeRecheckArmed) { showMintHome(); return; }
  openSwapper();
}

function renderSwapResults(s) {
  const pendingAccepts = s.results.filter((r) => !r.modified);
  const needsAccept = pendingAccepts.length > 0;
  // Only claim "sent to your Xaman app" when EVERY pending accept was pushed —
  // a partial batch would tell users to approve in-app and miss the QR-only ones.
  const allPushed = needsAccept && pendingAccepts.every((r) => r.accept_push === 'sent');
  el('swap-result-title').textContent = '🎉 Traits swapped!';
  el('swap-result-text').textContent = needsAccept
    ? signText(allPushed ? 'sent' : null, 'Scan each QR (or open in Xaman) to accept your re-crafted NFTs.')
    : 'Your NFTs were updated in place — the new traits are already in your wallet.';
  const box = el('swap-results');
  box.innerHTML = '';
  for (const r of s.results) {
    const div = document.createElement('div');
    div.className = 'swap-result';
    const h3 = document.createElement('h3');
    h3.textContent = r.name;
    const art = mediaEl({
      image: r.image_url, video: r.video_url, className: 'result-img', alt: r.name,
    });
    div.replaceChildren(h3, art);
    if (r.modified) {
      // Updated via NFTokenModify — nothing to accept.
      const note = document.createElement('span');
      note.className = 'modified-note';
      note.textContent = '✅ Updated in your wallet — no action needed.';
      div.appendChild(note);
    } else {
      const qrImg = document.createElement('img');
      qrImg.className = 'result-qr';
      qrImg.alt = 'QR';
      const btn = document.createElement('button');
      btn.className = 'link';
      btn.textContent = 'Open in Xaman';
      const toggle = makeQrToggle();
      div.appendChild(toggle);
      div.appendChild(qrImg);
      div.appendChild(btn);
      // #142: claim-your-swapped-NFT is a fresh sign ask; auto-open applies.
      applySignDelivery({ qrEl: qrImg, linkBtn: btn, toggleBtn: toggle,
        link: r.accept_deeplink, qrData: r.accept_deeplink, push: r.accept_push });
    }
    // The traits are already final on-chain at this point regardless of
    // `modified` (see run_swap_session: results are only appended once
    // everything is settled) — share per result, not once for the whole
    // panel, since a swap can touch up to two NFTs (#41 T9). Skipped when
    // shareUrlFor degrades to '' (no base known — dead link otherwise).
    const swapShareUrl = shareUrlFor(r.nft_number, r.nft_id);
    if (swapShareUrl) {
      div.appendChild(buildShareControl(swapShareText(r.nft_number), swapShareUrl, { kind: 'swap', nftNumber: r.nft_number }));
    }
    box.appendChild(div);
  }
  el('swap-done-btn').hidden = false;
}

function pollSwap(sessionId) {
  clearTimeout(swapPollTimer);
  swapExitHandled = false; // new chain, new cancellation to (maybe) exit from
  const gen = ++swapPollGen;
  const tick = async () => {
    if (gen !== swapPollGen) return; // superseded by a newer poll chain
    let s;
    try {
      s = await api(`/api/swap/${sessionId}`);
    } catch (e) {
      if (gen === swapPollGen) swapPollTimer = setTimeout(tick, 3000); // transient; keep polling
      return;
    }
    if (gen !== swapPollGen) return; // a newer chain started while we awaited
    if (s.state === 'cancelled') { exitSwapAfterCancel(); return; } // cancelled elsewhere
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

// thumb=1 serves the pre-generated 512px preview tier (animated layers as
// GIF, so they render in a plain <img> — Discord's webview can't play
// WebM/MP4 there). Missing thumbs fall back server-side to the full asset.
function layerSrc(body, trait, value) {
  return `${API_BASE}/api/layer?body=${encodeURIComponent(body)}` +
         `&trait=${encodeURIComponent(trait)}&value=${encodeURIComponent(value)}&thumb=1`;
}

// A backend trait-layer URL (/api/layer?...) is same-origin art that must NOT
// go through the CDN proxy (imgUrl). But on the standalone web surface the API
// is cross-origin, so — exactly like every other API call — a relative
// /api/layer path still needs the API_BASE prefix or it resolves against the
// Pages host and 404s. layerSrc() builds these client-side; the shop and
// marketplace receive them pre-built from the backend as image_url. Route those
// server-built URLs through here so they carry API_BASE too.
function traitLayerSrc(url) {
  return url ? API_BASE + url : null;
}

// WebM layers (VP9-alpha bodies) don't render in <img> — browsers only decode
// video containers in <video>. The client can't know a layer's format ahead of
// the fetch (/api/layer resolves the extension server-side), so build an <img>
// and, on error, retry once as a muted looping <video> in place. Only when the
// video ALSO errors is the layer genuinely missing — then onMissing fires (the
// closet/trait tiles use it to prune themselves, exactly like their old
// img.onerror did).
function layerMediaEl(src, alt, onMissing) {
  const img = document.createElement('img');
  img.src = src;
  img.alt = alt;
  img.onerror = () => {
    const v = document.createElement('video');
    // A browser/webview without VP9-in-WebM decode would fire the video's
    // onerror for a perfectly valid layer and (via onMissing) prune legit
    // tiles. When the codec is unsupported, show a blank placeholder instead —
    // never prune on a client-side capability gap.
    if (!v.canPlayType('video/webm; codecs="vp9"')) {
      img.onerror = null;
      img.src = BLANK_IMG;
      return;
    }
    v.autoplay = true;
    v.muted = true;
    v.loop = true;
    v.playsInline = true;
    v.setAttribute('aria-label', alt);
    if (onMissing) v.onerror = onMissing;
    v.src = src;
    img.replaceWith(v);
  };
  return img;
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
  // A blank (harvested) character has no traits to stack: render its own
  // silhouette metadata image and invite the user to build it.
  if (char.blank) {
    canvas.classList.remove('incomplete');
    if (char.image_url) {
      const img = document.createElement('img');
      img.src = imgUrl(char.image_url);
      img.alt = 'Blank character';
      canvas.appendChild(img);
    }
    el('dressup-id').textContent = `#${char.edition} · Blank — build me!`;
    return;
  }
  // Incomplete metadata (empty body) means every layer fetch would 400; show a
  // graceful "still indexing" state instead of a wall of broken images.
  if (!char.body) {
    canvas.classList.add('incomplete');
    el('dressup-id').textContent = `#${char.edition} · still indexing…`;
    return;
  }
  canvas.classList.remove('incomplete');
  // Draw staged (unsaved) changes, not just what is on-ledger.
  const shown = buildPure.applyPending(char.attributes, pending());
  const byType = Object.fromEntries(shown.map((a) => [a.trait_type, a.value]));
  for (const slot of order) {
    const value = byType[slot];
    if (!layerComplete(char.body, value)) continue;
    canvas.appendChild(layerMediaEl(layerSrc(char.body, slot, value), ''));
  }
  // #316: an unconfirmed save means this redraw may not match the ledger.
  el('dressup-id').textContent = reconcileUncertainIds.has(char.nft_id)
    ? `#${char.edition} · ${char.body} · ⚠️ save unconfirmed — refresh to re-check`
    : `#${char.edition} · ${char.body} · live`;
}

// --- GO picker (overlay) ---
// Replaces the old unlabeled bottom roster strip: a full-panel overlay grid
// of labeled tiles (#edition · body), opened from the Switch GO button.
let goAssembleEnabled = true;

// Batch-harvest (#356) multi-select: null = normal picker; an array = the
// selected nft_ids while the GO picker is in "pick characters to harvest" mode.
let batchSelect = null;
// Generation guard (same idiom as pollGen/bulkPollGen): bumps on every batch
// open/close so a batch POST response that was superseded by a close/reopen
// can never close the CURRENT picker or clobber its selection.
let batchGen = 0;
// One batch POST in flight at a time — the confirm button locks while pending.
let batchBusy = false;

function renderBatchBar() {
  const bar = el('go-picker-batch-bar');
  if (!bar) return; // stale cached index.html predating this id
  if (batchSelect === null) { bar.hidden = true; return; }
  bar.hidden = false;
  const summary = harvestPure.batchSummary(economyState.characters, batchSelect);
  const sumEl = el('go-picker-batch-summary');
  sumEl.textContent = summary.count === 0
    ? 'Tap characters to select them for harvest.'
    : `${summary.count} selected` + (summary.legacy ? ` · ${summary.legacy} need a Xaman tap` : ' · nothing to sign');
  const btn = el('go-picker-batch-confirm');
  btn.disabled = summary.count === 0 || batchBusy;
  btn.onclick = () => confirmBatchHarvest();
}

function renderGoPicker() {
  const grid = el('go-picker-grid');
  grid.replaceChildren();
  const batchMode = batchSelect !== null;
  for (const char of economyState.characters) {
    const t = buildPure.goTileState(char, activeNftId);
    const tile = document.createElement('button');
    tile.className = 'go-tile'
      + (t.state === 'active' ? ' active' : '')
      + (t.state === 'indexing' ? ' indexing' : '');
    const imgSrc = imgUrl(char.image_url, THUMB_W);
    const bodyVal = (char.attributes.find((a) => a.trait_type === 'Body') || {}).value;
    let media;
    if (imgSrc) {
      media = document.createElement('img');
      media.src = imgSrc;
      media.alt = t.label;
    } else if (layerComplete(char.body, bodyVal)) {
      media = layerMediaEl(layerSrc(char.body, 'Body', bodyVal), t.label);
    } else {
      // No CDN image and incomplete metadata: a layer fetch would 400.
      media = document.createElement('img');
      media.src = BLANK_IMG;
      media.alt = t.label;
    }
    media.loading = 'lazy';
    const cap = document.createElement('span');
    cap.className = 'go-tile-label';
    cap.textContent = t.state === 'active' ? `✓ ${t.label}` : t.label;
    const sub = document.createElement('span');
    sub.className = 'go-tile-sub';
    sub.textContent = t.sub; // goTileState already labels blanks "Blank — build me!"
    tile.replaceChildren(media, cap, sub);
    // #298: GO tiles stay static (image_url is the PNG first frame for
    // animated art); the badge flags GOs whose art plays as video.
    if (mediaPure.isAnimated(char)) {
      const anim = document.createElement('span');
      anim.className = 'anim-badge';
      anim.textContent = '▶';
      anim.setAttribute('aria-hidden', 'true');
      tile.appendChild(anim);
    }
    if (batchMode) {
      // Multi-select mode: tiles toggle membership instead of switching GOs.
      const selectable = harvestPure.harvestSelectable(char, [...harvestingIds]);
      if (!selectable) {
        tile.disabled = true;
        if (char.blank) sub.textContent = 'Already blank';
      } else {
        const selected = batchSelect.includes(char.nft_id);
        if (selected) {
          tile.style.outline = '3px solid var(--accent, #e91e63)';
          cap.textContent = `✓ ${t.label}`;
        }
        tile.onclick = () => {
          batchSelect = harvestPure.toggleSelected(batchSelect, char.nft_id);
          renderGoPicker();
        };
      }
      grid.appendChild(tile);
      continue;
    }
    if (t.state === 'indexing') {
      tile.disabled = true; // no body -> every layer fetch would 400
    } else {
      tile.onclick = async () => {
        // Reselecting the ALREADY-active GO is a no-op switch, not an exit —
        // prompting there would let a habitual "Discard" wipe a staged batch
        // that selectCharacter() would have kept anyway (same nft_id).
        const leaving = char.nft_id !== activeNftId;
        if (leaving && !(await confirmDiscardIfDirty())) return;
        closeGoPicker();
        selectCharacter(char.nft_id);
      };
    }
    grid.appendChild(tile);
  }
  renderBatchBar();
  if (batchMode) {
    el('go-picker-title').textContent = 'Harvest many';
    return; // no "Assemble new" tile while picking a harvest batch
  }
  el('go-picker-title').textContent = 'Your GOs';
  const add = document.createElement('button');
  add.className = 'go-tile assemble';
  // A bare ＋ with only a hover title reads as "add a GO" on touch devices —
  // label it in-tile like the character tiles.
  const plus = document.createElement('span');
  plus.textContent = '＋';
  plus.className = 'go-tile-plus';
  const cap = document.createElement('span');
  cap.className = 'go-tile-label';
  cap.textContent = goAssembleEnabled ? 'Assemble new' : 'Needs a Closet';
  add.replaceChildren(plus, cap);
  add.title = goAssembleEnabled ? 'Assemble new' : 'Create your Closet first';
  if (goAssembleEnabled) add.onclick = async () => {
    if (!(await confirmDiscardIfDirty())) return;
    closeGoPicker();
    openAssemble();
  };
  else add.disabled = true;
  grid.appendChild(add);
}

function openGoPicker() {
  const overlay = el('go-picker-overlay');
  if (!overlay.hidden) return; // already open — don't stack a 2nd keydown listener
  renderGoPicker();
  overlay.hidden = false;
  const onKey = (e) => { if (e.key === 'Escape') closeGoPicker(); };
  overlay._onKey = onKey; // stashed so closeGoPicker can remove it
  document.addEventListener('keydown', onKey);
  el('go-picker-close').onclick = () => closeGoPicker();
  overlay.onclick = (e) => { if (e.target === overlay) closeGoPicker(); }; // backdrop = close
}

function closeGoPicker() {
  const overlay = el('go-picker-overlay');
  batchGen++; // invalidate any in-flight batch response handler
  batchSelect = null; // leaving the picker always exits multi-select mode
  renderBatchBar();
  overlay.hidden = true;
  overlay.onclick = null;
  if (overlay._onKey) {
    document.removeEventListener('keydown', overlay._onKey);
    overlay._onKey = null;
  }
}

function selectCharacter(nftId) {
  if (nftId !== pendingFor) clearPending();
  activeNftId = nftId;
  const char = economyState.characters.find((c) => c.nft_id === nftId);
  if (char) renderCanvas(char);
  renderCloset();
}

// Returns the Closet issuance status from the nested token path.
// economyState.closet.token.status is the authoritative key (not .closet.status).
function closetStatus() {
  return (economyState.closet && economyState.closet.token && economyState.closet.token.status) || 'none';
}

// #386: a transient upstream failure while minting the Closet comes back as a
// structured retryable 503 ({error: 'closet_mint_transient', retryable: true}).
// ensure_closet is idempotent server-side, so clicking again is always safe —
// say so instead of showing a generic dead failure.
function closetFailure(e, gateBtn) {
  if (e.body && e.body.retryable) {
    showError('Temporary problem creating your Closet — nothing was lost. Please try again.');
    gateBtn.textContent = 'Try again';
  } else {
    showError(e.message);
  }
  gateBtn.disabled = false;
  status('');
}

async function openDressup() {
  showPanel('dressup-panel');
  clearPending();
  status('Loading your wardrobe…');
  try {
    economyState = await api('/api/economy');
    // #316: a fresh wardrobe fetch is the reconcile point — by now the
    // listener/admin reconcile has (or will have) stamped the index, so the
    // next authoritative read is trustworthy. Clear the unconfirmed-save flags.
    reconcileUncertainIds.clear();
    status('');

    const cStatus = closetStatus();
    const gate = el('closet-gate');
    const gateMsg = el('closet-gate-msg');
    const gateBtn = el('closet-gate-btn');
    const harvestBtn = el('dressup-harvest-btn');
    // Tolerate a stale cached index.html predating the batch button (#356).
    const harvestManyBtn = el('dressup-harvest-many-btn');

    // Hide the Dressing Room (canvas, Closet grid, trait strip) while gated —
    // otherwise the empty canvas and unpopulated closet-filter <select> render
    // beneath the gate.
    // Tolerate a stale cached index.html that predates these ids (Discord
    // clients are known to serve mixed asset versions).
    for (const id of ['dressup-main', 'trait-strip-section']) {
      const node = el(id);
      if (node) node.hidden = cStatus !== 'active';
    }

    if (cStatus !== 'active') {
      // Show gate; hide/disable Harvest. Reset the gate button: it gets disabled
      // while a POST /api/closet is in flight, and the same persistent DOM node
      // is reused when we re-render the gate (e.g. still pending_accept).
      gate.hidden = false;
      gateBtn.disabled = false;
      harvestBtn.disabled = true;
      harvestBtn.hidden = true;
      if (harvestManyBtn) { harvestManyBtn.disabled = true; harvestManyBtn.hidden = true; }

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
                text: signText(r.accept_push, 'Scan to accept your Closet in Xaman.'),
                qrData: r.accept, link: r.accept, push: r.accept_push, done: true });
            }
            economyState = await api('/api/economy');
            openDressup();
          } catch (e) {
            closetFailure(e, gateBtn);
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
                text: signText(r.accept_push, 'Scan to accept your Closet in Xaman.'),
                qrData: r.accept, link: r.accept, push: r.accept_push, done: true });
            }
            economyState = await api('/api/economy');
            openDressup();
          } catch (e) {
            closetFailure(e, gateBtn);
          }
        };
      }

      goAssembleEnabled = false;
      el('dressup-canvas').replaceChildren();
      return;
    }

    // Closet active — full Dressing Room
    gate.hidden = true;
    harvestBtn.disabled = false;
    harvestBtn.hidden = false;
    harvestBtn.onclick = () => harvestActive();
    if (harvestManyBtn) {
      harvestManyBtn.disabled = false;
      harvestManyBtn.hidden = false;
      harvestManyBtn.onclick = () => openBatchHarvest();
    }

    economyState.characters = economyState.characters.filter((c) => !harvestingIds.has(c.nft_id));
    goAssembleEnabled = true;
    activeNftId = buildPure.pickDefaultCharacter(economyState.characters);
    if (activeNftId) selectCharacter(activeNftId);
    else { el('dressup-canvas').replaceChildren(); renderCloset(); }
  } catch (e) {
    showError(e.message);
  }
}

let closetFilter = 'All';
let saveBusy = false;
// #316: characters whose last equip save ended with an UNKNOWN on-ledger
// outcome (equip_sync_indeterminate / failed_revert). The index was deliberately
// not stamped on those branches, so a redraw from /api/economy is not
// authoritative — banner the character and refuse staging/saving until the next
// wardrobe refresh (openDressup) returns fresh state. Same pattern as
// harvestingIds.
const reconcileUncertainIds = new Set();
const RECONCILE_MSG =
  "We couldn't confirm your save on the ledger — your character may or may not "
  + 'be wearing the new traits. Support is reconciling. Refresh to re-check; '
  + "don't re-save until then.";
let pendingEquips = {};   // {slot: incomingValue} — staged, uncommitted
let pendingFor = null;    // nft_id the staged batch belongs to
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
  const staged = pending();
  // A blank has no body, so the Closet can show it no trait art at all
  // (closetTileState hides every non-"None" tile) and equipping is a dead end.
  // Dressing a blank goes through Assemble, which picks the body first — this
  // button is the only door to it from a selected blank. renderCloset owns the
  // button because every selection path ends here, with or without a GO.
  renderBuildShareRow(char);
  const blankBtn = el('build-blank-btn');
  if (blankBtn) {
    blankBtn.hidden = !(char && char.blank);
    blankBtn.onclick = char && char.blank ? () => openAssemble(char.nft_id) : null;
  }
  if (char && char.blank) {
    // Without the hint the Closet reads as broken ("where did my traits go?").
    const hint = document.createElement('p');
    hint.className = 'closet-blank-hint';
    hint.textContent = 'Pick a body first — hit “Build this GO” to dress this blank.';
    grid.appendChild(hint);
  }
  for (const asset of buildPure.effectiveAssets(economyState.closet.assets, char, staged)) {
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
    // With a GO selected, a trait that can't render on its body is hidden
    // entirely (it reappears on a GO whose body has the art). With no GO
    // selected — and for the always-visible "None" asset — keep a blank
    // placeholder so the Closet contents stay visible.
    const tile = buildPure.closetTileState(asset, char);
    if (!tile.visible) continue;
    let img;
    if (tile.art === 'layer') {
      // Art missing for this body (layer fetch 404s even as video): drop the
      // whole tile instead of rendering a broken image.
      img = layerMediaEl(
        layerSrc(char.body, asset.slot, asset.value),
        `${asset.slot}: ${asset.value}`,
        () => item.remove(),
      );
    } else {
      img = document.createElement('img');
      img.src = BLANK_IMG;
      img.alt = `${asset.slot}: ${asset.value}`;
    }
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = `×${asset.count}`;
    // Extract button: pull this loose trait out as a tradeable NFToken.
    const extractBtn = document.createElement('button');
    extractBtn.className = 'extract';
    extractBtn.title = 'Extract as tradeable trait';
    extractBtn.textContent = '↑';
    // Extract mutates the very Closet counts the staged batch is computed
    // against — block it until the batch is saved or discarded.
    if (isDirty()) {
      extractBtn.disabled = true;
      extractBtn.title = 'Save or discard your changes first';
    }
    extractBtn.onclick = (e) => {
      e.stopPropagation();  // don't also fire the tile equip click
      extractTrait(asset.slot, asset.value, extractBtn);
    };
    const parts = [img];
    if (tile.label) {
      // A blank tile needs words: "None" art is an empty image, so without a
      // caption a harvested empty slot reads as a rendering failure.
      const caption = document.createElement('span');
      caption.className = 'closet-caption';
      caption.textContent = `${asset.slot}\n${tile.label}`;
      parts.push(caption);
    }
    item.replaceChildren(...parts, count, extractBtn);
    if (staged[asset.slot] === asset.value) item.classList.add('staged');
    // Equip is wired only when the asset is compatible with the active character;
    // the tile still renders (and Extract still works) when it isn't.
    // Staging only — nothing goes on-ledger until Save.
    if (compatible) item.onclick = () => stagePendingEquip(asset.slot, asset.value);
    grid.appendChild(item);
  }
  renderSaveBar();
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
    const tile = buildPure.closetTileState(t, char);
    if (!tile.visible) continue;   // a "None" token stays listed — it must remain depositable
    const chip = document.createElement('div');
    chip.className = 'trait-chip';
    let img;
    if (tile.art === 'layer') {
      img = layerMediaEl(
        layerSrc(char.body, t.slot, t.value),
        `${t.slot}: ${t.value}`,
        () => chip.remove(),
      );
    } else {
      img = document.createElement('img');
      img.src = BLANK_IMG;
      img.alt = `${t.slot}: ${t.value}`;
    }
    const label = document.createElement('span');
    label.className = 'trait-chip-label';
    label.textContent = `${t.slot}: ${t.value}`;
    const depositBtn = document.createElement('button');
    depositBtn.className = 'deposit';
    depositBtn.textContent = 'Deposit';
    if (isDirty()) {
      depositBtn.disabled = true;
      depositBtn.title = 'Save or discard your changes first';
    }
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
        text: signText(final.accept_push, 'Scan to accept your tradeable trait in Xaman.'),
        qrData: final.accept,
        link: final.accept,
        push: final.accept_push,
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

// Staged (unsaved) Build helpers. A tile click no longer transacts: it records
// the change, repaints from the pending model, and surfaces the Save bar. The
// whole batch commits in ONE NFTokenModify when the user clicks Save.

function pending() {
  return pendingFor === activeNftId ? pendingEquips : {};
}

// The nft_id of the most recently SAVED build — the only character whose
// inline share row shows. Cleared when the user stages new (unsaved) changes,
// so the row can never advertise a look that is no longer on-ledger.
let buildShareFor = null;

function isDirty() {
  const char = activeChar();
  return Boolean(char) && buildPure.netChanges(char, pending()).length > 0;
}

function clearPending() {
  pendingEquips = {};
  pendingFor = null;
}

function stagePendingEquip(slot, value) {
  const char = activeChar();
  if (!char || saveBusy) return;
  if (reconcileUncertainIds.has(activeNftId)) { showError(RECONCILE_MSG); return; }
  if (pendingFor !== activeNftId) { pendingEquips = {}; pendingFor = activeNftId; }
  buildShareFor = null;   // staged edits supersede the saved look
  pendingEquips[slot] = value;
  renderCanvas(char);
  renderCloset();
}

// The inline share row for a just-saved build. Shown only for the character
// that was saved, and only once its art is on-ledger — buildShareControl is
// skipped entirely when shareUrlFor knows no base (a dead link is worse than
// no button), exactly like the mint/swap/assemble flow panels.
function renderBuildShareRow(char) {
  const row = el('dressup-share-row');
  if (!row) return;   // tolerate a stale cached index.html
  row.replaceChildren();
  const show = Boolean(char) && buildShareFor === char.nft_id;
  const url = show ? shareUrlFor(char.edition, char.nft_id) : '';
  row.hidden = !url;
  if (!url) return;
  row.appendChild(buildShareControl(
    equipShareText(char.edition),
    url,
    { kind: 'equip', nftNumber: char.edition },
  ));
}

function renderSaveBar() {
  const bar = el('build-save-bar');
  if (!bar) return;   // tolerate a stale cached index.html
  const char = activeChar();
  const n = char ? buildPure.netChanges(char, pending()).length : 0;
  bar.hidden = n === 0;
  el('build-save-btn').textContent = `💾 Save changes (${n})`;
  el('build-save-btn').disabled = saveBusy;
  el('build-discard-btn').disabled = saveBusy;
}

function discardPending() {
  // Defense-in-depth: renderSaveBar() disables the Discard button while a
  // save is in flight, but this must not be the only guard — a stale cached
  // index.html (renderSaveBar tolerates its own missing #build-save-bar) or
  // any other caller must never clear pendingEquips out from under a
  // running saveBuild().
  if (saveBusy) return;
  clearPending();
  const char = activeChar();
  if (char) renderCanvas(char);
  renderCloset();
}

// Every exit from the current character routes through here. Returns true when
// it is safe to proceed (nothing staged, or the user chose to discard).
// Native window.confirm is a silent no-op in Discord's sandboxed iframe.
async function confirmDiscardIfDirty() {
  // A save is already in flight — pendingEquips isn't cleared until saveBuild's
  // finally, so isDirty() would still read true here. Refuse the exit instead of
  // offering "Discard" (which would lie about the in-flight batch never landing).
  if (saveBusy) {
    showError('Still saving your build — please wait a moment.');
    return false;
  }
  if (!isDirty()) return true;
  const char = activeChar();
  const ok = await confirmDialog({
    title: 'Discard unsaved changes?',
    text: `You have unsaved changes to #${char.edition}. They have not been saved to the ledger.`,
    confirmLabel: 'Discard',
  });
  if (ok) discardPending();
  return ok;
}

// Fold a just-committed batch into the in-memory state, reusing the same pure
// helpers the staged preview uses. Only ever called after the ledger accepted
// the batch, so this is truth, not optimism.
function applySavedLocally(char, saved) {
  const assets = economyState.closet && economyState.closet.assets;
  // Closet first: effectiveAssets needs the character's PRE-change values.
  if (assets) economyState.closet.assets = buildPure.effectiveAssets(assets, char, saved);
  char.attributes = buildPure.applyPending(char.attributes, saved);
}

async function saveBuild() {
  const char = activeChar();
  if (!char || saveBusy) return;
  if (reconcileUncertainIds.has(activeNftId)) { showError(RECONCILE_MSG); return; }
  // The user can switch characters while the poll below is pending — pin the
  // submitted NFT ID now so the request and the uncertain flag can never target
  // a character other than the one this batch was saved against.
  const submittedNftId = activeNftId;
  const staged = { ...pending() };
  const changes = buildPure.netChanges(char, staged);
  if (!changes.length) return;
  saveBusy = true;
  renderSaveBar();
  status('Saving your build…');
  let committed = false;
  try {
    const res = await api('/api/equip', {
      method: 'POST',
      body: JSON.stringify({ nft_id: submittedNftId, changes }),
    });
    const final = await pollEconomyOp('equip', res);
    const outcome = buildPure.saveOutcome(final);
    if (outcome === 'uncertain') {
      // The ledger outcome is UNKNOWN (equip_sync_indeterminate / failed_revert):
      // the index was not stamped, so the refetch below redraws a look that may
      // be wrong. Flag the character so the redraw carries a banner and further
      // staging/saving is refused until a wardrobe refresh.
      reconcileUncertainIds.add(submittedNftId);
      throw new Error(RECONCILE_MSG);
    }
    if (final.state === 'failed') throw new Error(final.error || 'save failed');
    committed = true;
    status('');
  } catch (e) {
    showError(e.message);
    status('');
  } finally {
    // Always resync from authoritative state and drop the staged batch — the
    // indeterminate / mirror-pending branches can leave the character genuinely
    // changed, so silently re-offering the same batch could double-apply it.
    saveBusy = false;
    clearPending();
    // The batch is on-ledger: fold it into local state BEFORE the refetch, so a
    // refetch that FAILS still shows the new look instead of silently redrawing
    // the pre-save character and reading as a lost save. A refetch that
    // succeeds overwrites this with authoritative truth anyway.
    if (committed) {
      applySavedLocally(char, staged);
      buildShareFor = submittedNftId;   // armed before the redraw below
    }
    try {
      economyState = await api('/api/economy');
    } catch (e) {
      showError(e.message);
    }
    selectCharacter(activeNftId);
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

// nft_ids with a harvest in flight (fire-and-forget, spec 2026-07-21). Used to
// keep a burned-in-progress character out of the selectable set on re-render.
const harvestingIds = new Set();
// Monotonic sequence for tracker-driven economy refetches: concurrent
// trackHarvest completions can resolve /api/economy out of order — only the
// newest fetch may overwrite economyState.
let economyRefreshSeq = 0;

async function harvestActive() {
  const char = activeChar();
  if (!char) return;
  if (!(await confirmDiscardIfDirty())) return;
  // A mutable char is stripped to a blank in place (no burn, nothing to sign);
  // a legacy non-mutable one must be burned and re-minted as a blank, which
  // costs the user one Xaman accept — keep the heavier warning for it.
  const confirmOpts = char.mutable
    ? {
        title: 'Strip this character down?',
        text: `This strips #${char.edition} to a blank. Its parts go to your Closet; the NFT stays in your wallet.`,
        confirmLabel: '🧺 Harvest',
      }
    : {
        title: 'Harvest this character?',
        text: `#${char.edition} predates Dynamic NFTs: harvesting burns and re-mints it as a blank (one Xaman accept), then its parts go to your Closet.`,
        confirmLabel: '🔥 Harvest',
      };
  if (!(await confirmDialog(confirmOpts))) return;
  let res;
  try {
    res = await api('/api/harvest', {
      method: 'POST', body: JSON.stringify({ nft_id: char.nft_id }),
    });
  } catch (e) {
    showError(e.message);
    return;
  }
  // Fire-and-forget: drop the character from the local roster immediately so
  // the user can select + harvest the next one; the tracker below reconciles
  // real state when the op lands. Never navigate the user anywhere.
  harvestingIds.add(char.nft_id);
  economyState.characters = economyState.characters.filter((c) => c.nft_id !== char.nft_id);
  toast(`🔥 Harvesting #${char.edition} — keep playing, this takes a moment.`);
  if (activeNftId === char.nft_id) {
    activeNftId = buildPure.pickDefaultCharacter(economyState.characters);
    if (!el('dressup-panel').hidden) {
      if (activeNftId) selectCharacter(activeNftId);
      else { el('dressup-canvas').replaceChildren(); renderCloset(); }
    }
  }
  trackHarvest(char, res);
}

// Batch harvest (#356): open the GO picker in multi-select mode.
async function openBatchHarvest() {
  if (!(await confirmDiscardIfDirty())) return;
  batchGen++; // a fresh selection round supersedes any older response handler
  batchSelect = [];
  openGoPicker();
  renderGoPicker(); // re-render in batch mode even if the overlay was open
}

async function confirmBatchHarvest() {
  if (batchBusy) return; // one batch POST at a time
  const summary = harvestPure.batchSummary(economyState.characters, batchSelect);
  if (!summary.count) return;
  const ok = await confirmDialog({
    title: summary.count === 1 ? 'Harvest this character?' : `Harvest ${summary.count} characters?`,
    text: harvestPure.confirmText(summary),
    confirmLabel: summary.legacy ? '🔥 Harvest all' : '🧺 Harvest all',
  });
  if (!ok) return;
  if (batchSelect === null) return; // picker closed while the dialog was up
  const selected = batchSelect.slice();
  // Generation guard (P1, #376/#378 idiom): capture the round this POST
  // belongs to. If the user closes the picker and opens a new selection while
  // the request is pending, this handler must not touch the new picker.
  const gen = batchGen;
  batchBusy = true;
  renderBatchBar();
  let res;
  try {
    res = await api('/api/harvest/batch', {
      method: 'POST', body: JSON.stringify({ nft_ids: selected }),
    });
  } catch (e) {
    showError(e.message);
    return;
  } finally {
    batchBusy = false;
    renderBatchBar();
  }
  const fresh = gen === batchGen;
  if (fresh) closeGoPicker(); // also resets batchSelect; superseded -> hands off
  const { started, rejected } = harvestPure.splitBatchResults(res.results);
  // Fire-and-forget per started unit — exactly the single-harvest pattern:
  // drop each from the roster now, poll each session, reconcile when it lands.
  for (const r of started) {
    const char = economyState.characters.find((c) => c.nft_id === r.nft_id)
      || { nft_id: r.nft_id, edition: '?' };
    harvestingIds.add(r.nft_id);
    trackHarvest(char, { id: r.session_id, state: r.state });
  }
  economyState.characters = economyState.characters.filter((c) => !harvestingIds.has(c.nft_id));
  if (!fresh && batchSelect !== null) {
    // A newer selection round is open: drop any id this batch just started
    // and refresh its tiles/summary — but leave the picker itself alone.
    batchSelect = harvestPure.pruneSelection(batchSelect, [...harvestingIds]);
    renderGoPicker();
  }
  if (started.length) {
    toast(`🔥 Harvesting ${started.length} character${started.length === 1 ? '' : 's'} — keep playing, this takes a moment.`);
  }
  if (rejected.length) {
    showError(rejected.map((r) => `Could not harvest ${labelForNft(r.nft_id)}: ${r.error}`).join('\n'));
  }
  if (harvestingIds.has(activeNftId)) {
    activeNftId = buildPure.pickDefaultCharacter(economyState.characters);
    if (!el('dressup-panel').hidden) {
      if (activeNftId) selectCharacter(activeNftId);
      else { el('dressup-canvas').replaceChildren(); renderCloset(); }
    }
  }
}

function labelForNft(nftId) {
  const char = economyState.characters.find((c) => c.nft_id === nftId);
  return char && char.edition != null ? `#${char.edition}` : 'a character';
}

async function trackHarvest(char, startResp) {
  const final = await pollEconomyOp('harvest', startResp);
  harvestingIds.delete(char.nft_id);
  if (final.state === 'failed') {
    showError(`Harvest of #${char.edition} failed: ${final.error || 'unknown error'}`);
  } else {
    toast(`✅ #${char.edition} harvested — parts added to your Closet.`);
    // Legacy upgrade path: the re-mint produced a fresh blank the user must
    // accept in Xaman — surface its QR (mutable strips have no accept).
    if (final.accept) {
      showFlow({
        title: '👛 Accept your upgraded blank',
        text: signText(final.accept_push, 'Accept your upgraded blank in Xaman.'),
        qrData: final.accept,
        link: final.accept,
        push: final.accept_push,
        done: true,
      });
    }
  }
  // Reconcile real state silently; re-render ONLY if the Dressing Room is the
  // visible panel — never yank the user out of another flow (e.g. a mint).
  const seq = ++economyRefreshSeq;
  try {
    const fresh = await api('/api/economy');
    if (seq !== economyRefreshSeq) return; // a newer refetch superseded this one
    economyState = fresh;
  } catch (e) {
    return; // transient; next openDressup() refetches anyway
  }
  economyState.characters = economyState.characters.filter((c) => !harvestingIds.has(c.nft_id));
  if (el('dressup-panel').hidden) return;
  if (!economyState.characters.find((c) => c.nft_id === activeNftId)) {
    activeNftId = buildPure.pickDefaultCharacter(economyState.characters);
  }
  if (activeNftId) selectCharacter(activeNftId);
  else { el('dressup-canvas').replaceChildren(); renderCloset(); }
}

// `preselectNftId` (optional): the blank the user came from via "Build this GO".
async function openAssemble(preselectNftId) {
  let opts;
  try {
    opts = await api('/api/assemble/options');
  } catch (e) {
    showError(e.message);
    return;
  }
  if (!opts.blanks.length) {
    showError('No blank characters to assemble — harvest a character first.');
    return;
  }
  if (!opts.bodies.length) {
    showError('Your Closet has no bodies — harvest a character first.');
    return;
  }
  openBuilder(opts, preselectNftId);
}

// --- Assemble builder overlay ---
// A three-step, fully client-side wizard: pick a blank -> pick a body -> pick
// a trait per slot beside a live stacked preview. The server's
// /api/assemble/options payload is the single source of truth: `options` is
// keyed by body VALUE, `body_class` maps that value to its layer-dir class so
// the preview never guesses. Commit POSTs {nft_id, body, chosen}; nothing is
// signed (a mutable blank is dressed in place).
let builderState = null;

// The blank's silhouette art: options.blanks carry only {nft_id, edition}, so
// cross-reference the loaded roster for the character's own metadata image
// (the silhouette), falling back to a transparent placeholder.
function blankImgSrc(nftId) {
  const c = economyState && economyState.characters.find((x) => x.nft_id === nftId);
  return c && c.image_url ? imgUrl(c.image_url, THUMB_W) : BLANK_IMG;
}

function openBuilder(opts, preselectNftId) {
  const overlay = el('builder-overlay');
  if (!overlay.hidden) return; // already open
  builderState = {
    opts,
    // The blank the user came from, else the lone blank, else step 1 picks.
    blank: buildPure.pickBuilderBlank(opts.blanks, preselectNftId),
    body: null,
    chosen: {},
  };
  overlay.hidden = false;
  const onKey = (e) => { if (e.key === 'Escape') closeBuilder(); };
  overlay._onKey = onKey;
  document.addEventListener('keydown', onKey);
  el('builder-close').onclick = () => closeBuilder();
  overlay.onclick = (e) => { if (e.target === overlay) closeBuilder(); }; // backdrop = close
  renderBuilder();
}

function closeBuilder() {
  const overlay = el('builder-overlay');
  overlay.hidden = true;
  overlay.onclick = null;
  if (overlay._onKey) {
    document.removeEventListener('keydown', overlay._onKey);
    overlay._onKey = null;
  }
  builderState = null;
}

function builderStepTitle(text) {
  const h = document.createElement('h3');
  h.className = 'builder-step-title';
  h.textContent = text;
  return h;
}

function builderBlankStep() {
  const { opts, blank } = builderState;
  const wrap = document.createElement('div');
  wrap.className = 'builder-step';
  wrap.appendChild(builderStepTitle('1 · Pick a blank'));
  const grid = document.createElement('div');
  grid.className = 'go-picker-grid';
  for (const b of opts.blanks) {
    const tile = document.createElement('button');
    tile.className = 'go-tile' + (blank && blank.nft_id === b.nft_id ? ' active' : '');
    const img = document.createElement('img');
    img.src = blankImgSrc(b.nft_id);
    img.alt = `#${b.edition}`;
    img.loading = 'lazy';
    const cap = document.createElement('span');
    cap.className = 'go-tile-label';
    cap.textContent = `#${b.edition}`;
    tile.replaceChildren(img, cap);
    tile.onclick = () => { builderState.blank = b; renderBuilder(); };
    grid.appendChild(tile);
  }
  wrap.appendChild(grid);
  return wrap;
}

function builderBodyStep() {
  const { opts, body } = builderState;
  const wrap = document.createElement('div');
  wrap.className = 'builder-step';
  wrap.appendChild(builderStepTitle('2 · Pick a body'));
  const grid = document.createElement('div');
  grid.className = 'go-picker-grid';
  for (const b of opts.bodies) {
    const cls = opts.body_class[b];
    const tile = document.createElement('button');
    tile.className = 'go-tile' + (body === b ? ' active' : '');
    const media = layerMediaEl(layerSrc(cls, 'Body', b), b);
    media.loading = 'lazy';
    const cap = document.createElement('span');
    cap.className = 'go-tile-label';
    cap.textContent = b;
    tile.replaceChildren(media, cap);
    tile.onclick = () => {
      builderState.body = b;
      // Switching body re-defaults the chosen set to that body's first legal
      // value per slot (a previous body's picks may not be legal here).
      builderState.chosen = buildPure.defaultChosen(opts.slots, opts.options[b] || {});
      renderBuilder();
    };
    grid.appendChild(tile);
  }
  wrap.appendChild(grid);
  return wrap;
}

function builderTraitStep() {
  const { opts, body } = builderState;
  const slotOptions = opts.options[body] || {};
  const wrap = document.createElement('div');
  wrap.className = 'builder-step builder-trait-step';
  wrap.appendChild(builderStepTitle('3 · Choose traits'));

  const layout = document.createElement('div');
  layout.className = 'builder-trait-layout';

  const preview = document.createElement('div');
  preview.className = 'builder-preview';
  preview.id = 'builder-preview';

  const pickers = document.createElement('div');
  pickers.className = 'builder-pickers';
  for (const slot of opts.slots) {
    const row = document.createElement('label');
    row.className = 'builder-picker';
    const name = document.createElement('span');
    name.className = 'builder-picker-label';
    name.textContent = slot;
    const sel = document.createElement('select');
    const vals = slotOptions[slot] || [];
    if (vals.length) {
      for (const v of vals) {
        const o = document.createElement('option');
        o.value = v; o.textContent = v;
        sel.appendChild(o);
      }
      if (builderState.chosen[slot] != null) sel.value = builderState.chosen[slot];
      sel.onchange = () => { builderState.chosen[slot] = sel.value; refreshBuilderPreview(); };
    } else {
      const o = document.createElement('option');
      o.textContent = '— none available —';
      sel.appendChild(o);
      sel.disabled = true;
    }
    row.replaceChildren(name, sel);
    pickers.appendChild(row);
  }
  layout.replaceChildren(preview, pickers);
  wrap.appendChild(layout);

  const warn = document.createElement('p');
  warn.className = 'builder-warn';
  warn.id = 'builder-warn';
  warn.hidden = true;
  wrap.appendChild(warn);

  const assembleBtn = document.createElement('button');
  assembleBtn.id = 'builder-assemble-btn';
  assembleBtn.className = 'primary';
  assembleBtn.textContent = 'Assemble';
  assembleBtn.onclick = async () => {
    // Keep the builder (and its selections) alive until the op actually
    // succeeds — a transient failure must not discard the user's picks.
    const b = builderState.blank;
    const bodyVal = builderState.body;
    const chosen = { ...builderState.chosen };
    assembleBtn.disabled = true;
    assembleBtn.textContent = 'Assembling…';
    const ok = await commitAssemble(b.nft_id, bodyVal, chosen, b.edition);
    if (ok) { closeBuilder(); return; }
    assembleBtn.disabled = false;
    assembleBtn.textContent = 'Assemble';
  };
  wrap.appendChild(assembleBtn);
  return wrap;
}

// Repaint the live preview stack + missing-slot warning + Assemble enabled
// state. Reads elements by id, so it must run only after builderTraitStep's
// nodes are attached (renderBuilder calls it at the end).
function refreshBuilderPreview() {
  const preview = el('builder-preview');
  if (!preview) return;
  const { opts, body, chosen } = builderState;
  const cls = opts.body_class[body];
  const stack = [layerMediaEl(layerSrc(cls, 'Body', body), body)];
  for (const slot of opts.slots) {
    const v = chosen[slot];
    if (v && v !== 'None') stack.push(layerMediaEl(layerSrc(cls, slot, v), `${slot}: ${v}`));
  }
  preview.replaceChildren(...stack);

  const missing = buildPure.missingSlots(opts.slots, opts.options[body] || {});
  const warn = el('builder-warn');
  warn.hidden = missing.length === 0;
  if (missing.length) {
    warn.textContent = `Your Closet has no ${body}-compatible options for: ${missing.join(', ')}`;
  }
  el('builder-assemble-btn').disabled = missing.length > 0;
}

function renderBuilder() {
  const bodyEl = el('builder-body');
  bodyEl.replaceChildren();
  bodyEl.appendChild(builderBlankStep());
  if (builderState.blank) bodyEl.appendChild(builderBodyStep());
  if (builderState.blank && builderState.body) {
    bodyEl.appendChild(builderTraitStep());
    refreshBuilderPreview();
  }
}

// Returns true on success so the caller (the builder's Assemble button) knows
// whether to close the overlay or keep the user's selections for a retry.
async function commitAssemble(nftId, body, chosen, edition) {
  status('Assembling…');
  try {
    const res = await api('/api/assemble', {
      method: 'POST', body: JSON.stringify({ nft_id: nftId, body, chosen }),
    });
    const final = await pollEconomyOp('assemble', res);
    status('');
    if (final.state === 'failed') throw new Error(final.error || 'assemble failed');
    // Nothing to sign: a mutable blank is dressed in place. Show the new art.
    showFlow({
      title: `🎉 #${edition} assembled!`,
      text: 'Your character has been dressed — no signature needed.',
      image: final.image_url,
      video: final.video_url,
      done: true,
      celebrate: true,
      // Freshly built character: same share affordance as mint/swap. showFlow
      // hides the row itself when shareUrlFor finds no base (dead link).
      share: {
        text: assembleShareText(edition),
        url: shareUrlFor(edition, nftId),
        meta: { kind: 'assemble', nftNumber: edition },
      },
    });
  } catch (e) {
    status('');
    showError(e.message);
    return false;
  }
  // Past the point of no return: the blank IS dressed and its Closet assets
  // are spent. The roster refresh is best-effort — a failure here must never
  // report failure, or the builder would reopen with stale selections and
  // invite a double submission (the next openDressup() refetches anyway).
  try {
    economyState = await api('/api/economy');
  } catch (e) {
    // ignore: transient; state reconciles on the next economy fetch
  }
  return true;
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
  bid: (id) => `/api/market/bid/${id}`,
  bid_accept: (id) => `/api/market/bid/accept/${id}`,
  trait_list: (id) => `/api/market/trait/list/${id}`,
};

const marketState = { tab: 'browse', kind: 'character', offset: 0 };
let marketPendingItem = null; // the character/trait/closet-asset the list-form panel is acting on
let marketFlowTimer = null;
// Generation counter (mirrors pollMint's pollGen): clearTimeout alone cannot
// kill a tick already awaiting its status fetch — it would resume, repaint
// the shared flow-panel, and re-arm. Bumped on every pollMarketFlow start and
// by invalidateFlowPolls().
let marketFlowGen = 0;

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
  el('market-shop').hidden = true;
  highlightTabs('market-tabs', 'tab', 'browse');
  highlightTabs('market-kind', 'kind', marketState.kind);
  await loadMarketBrowse();
}

function switchMarketTab(tab) {
  if (tab === 'shop' && !shopEnabled) tab = 'browse';
  marketState.tab = tab;
  highlightTabs('market-tabs', 'tab', tab);
  el('market-browse').hidden = tab !== 'browse';
  el('market-mine').hidden = tab !== 'mine';
  el('market-shop').hidden = tab !== 'shop';
  if (tab === 'browse') loadMarketBrowse();
  else if (tab === 'mine') loadMarketMine();
  else loadShopCatalog();
}

// A trait-image URL from the backend (/api/layer?...) must NOT go through the
// CDN proxy (imgUrl); a character's `image` is an absolute CDN URL and must.
// The trait URL is same-origin under Discord/Telegram but cross-origin on the
// web surface, so it still needs API_BASE (traitLayerSrc). Mirrors the same
// distinction renderCanvas/renderCloset draw between layerSrc() and imgUrl().
function marketRowImgSrc(vm) {
  if (!vm.image) return null;
  return vm.kind === 'trait' ? traitLayerSrc(vm.image) : imgUrl(vm.image, THUMB_W);
}

// #203: append=true keeps existing cards ("Load more" pagination); every
// card click now opens the listing detail overlay (art, traits, rarity,
// price, seller, sale history) — Buy / link-out live inside the overlay.
function renderMarketGrid(rows, { append = false } = {}) {
  const grid = el('market-grid');
  const empty = el('market-empty');
  if (!append) grid.replaceChildren();
  if (!rows.length && !grid.childElementCount) { empty.hidden = false; return; }
  empty.hidden = true;
  for (const row of rows) {
    const vm = marketPure.mapListingRow(row);
    const card = document.createElement('button');
    card.className = 'nft-card';
    const img = document.createElement('img');
    img.src = marketRowImgSrc(vm) || BLANK_IMG;
    img.loading = 'lazy';
    img.alt = '';
    const name = document.createElement('span');
    name.className = 'cap';
    name.textContent = vm.title;
    const price = document.createElement('span');
    price.className = 'market-card-price';
    price.textContent = vm.priceLabel;
    name.appendChild(price);
    const rarity = marketPure.rarityLabel(vm);
    if (rarity) {
      const chip = document.createElement('span');
      chip.className = 'market-card-rarity';
      chip.textContent = rarity;
      name.appendChild(chip);
    }
    // #131: an external (brokered) listing renders as a visually distinct,
    // non-buyable card — "Listed on <marketplace>" badge; the detail overlay
    // links out instead of offering an in-app Buy.
    if (vm.external) {
      card.classList.add('market-card-external');
      const badge = document.createElement('span');
      badge.className = 'market-card-external-badge';
      badge.textContent = marketPure.externalLabel(vm);
      name.appendChild(badge);
    }
    card.replaceChildren(img, name);
    // #298: grid tiles stay static stills; the badge flags art that plays as
    // video in the listing-detail overlay (same marker as the swap grid).
    if (mediaPure.isAnimated(row)) {
      const anim = document.createElement('span');
      anim.className = 'anim-badge';
      anim.textContent = '▶';
      anim.setAttribute('aria-hidden', 'true');
      card.appendChild(anim);
    }
    // #133: async handler — route any throw to the toast surface.
    card.onclick = () => openListingDetail(row).catch((e) => showError(e.message));
    grid.appendChild(card);
  }
}

// --- #203: per-listing detail overlay ---

// a11y (#284 review): the element that opened the overlay, to restore focus
// on close; and the listing the overlay is currently showing, so a slow
// history fetch for an earlier listing can never paint into a newer one.
let lastListingTrigger = null;
let activeListingId = null;

function closeListingDetail() {
  // #298: free the decoder — an animated listing's <video> keeps playing
  // (and downloading) inside the hidden overlay otherwise.
  const art = el('listing-detail-img');
  if (art && art.tagName === 'VIDEO') art.pause();
  el('listing-overlay').hidden = true;
  el('listing-detail-action').onclick = null;
  activeListingId = null;
  if (lastListingTrigger) { lastListingTrigger.focus(); lastListingTrigger = null; }
}

function renderListingHistory(items) {
  const list = el('listing-detail-history');
  list.replaceChildren();
  el('listing-history-title').hidden = !items.length;
  for (const it of items.slice(0, 8)) {
    const li = document.createElement('li');
    const when = it.ts ? new Date(it.ts * 1000).toLocaleDateString() : '';
    const price = it.price_drops != null
      ? `${marketPure.dropsToXrpStr(String(it.price_drops))} XRP`
      : (it.amount_brix != null ? `${it.amount_brix} BRIX` : '');
    const label = it.event ? it.event.replace(/_/g, ' ') : 'sold';
    li.textContent = [label, price, when].filter(Boolean).join(' · ');
    list.appendChild(li);
  }
}

async function openListingDetail(row) {
  const vm = marketPure.mapListingRow(row);
  const requestId = vm.offerIndex || vm.nftId;
  activeListingId = requestId;
  lastListingTrigger = document.activeElement;
  // #298: the detail view is where animation belongs — upgrade to the MP4
  // (autoplay/loop/muted, preload=metadata, poster = the static still; a
  // playback error degrades back to the still inside mediaEl). Static rows
  // (and all traits, whose image is a /api/layer URL that must not go
  // through the imgUrl CDN proxy) keep the plain <img> exactly as before.
  const dm = mediaPure.detailMedia(row);
  if (dm.video) {
    setMedia('listing-detail-img', { image: dm.image, video: dm.video });
  } else {
    const still = setMedia('listing-detail-img', { image: dm.image });
    still.src = marketRowImgSrc(vm) || BLANK_IMG;
  }
  el('listing-detail-title').textContent = vm.title;
  el('listing-detail-price').textContent = vm.priceLabel;
  const sellerShort = vm.seller ? `${vm.seller.slice(0, 8)}…${vm.seller.slice(-4)}` : '';
  const rarity = marketPure.rarityLabel(vm);
  el('listing-detail-sub').textContent = [
    vm.badge,
    rarity,
    vm.external ? marketPure.externalLabel(vm) : '',
    sellerShort ? `Seller ${sellerShort}` : '',
  ].filter(Boolean).join(' · ');
  const attrs = el('listing-detail-attrs');
  attrs.replaceChildren();
  for (const a of row.attributes || []) {
    if (!a || !a.value || a.value === 'None') continue;
    const chip = document.createElement('span');
    chip.className = 'listing-attr-chip';
    chip.textContent = `${a.trait_type}: ${a.value}`;
    attrs.appendChild(chip);
  }
  const action = el('listing-detail-action');
  const extLink = el('listing-detail-external');
  const feeNote = el('listing-detail-fee');
  extLink.hidden = true;
  feeNote.hidden = true;
  const buyNow = marketPure.buyNowLabel(vm);
  if (vm.external && buyNow) {
    // #426: the broker's fee rate is measured, so the server computed the
    // minimum bid its bot will settle. Primary = Buy now (a plain native
    // bid at the clearing price; the broker's bot brokers the accept),
    // deep link demoted to a secondary action.
    action.textContent = buyNow;
    action.disabled = false;
    action.onclick = () => { buyExternalNow(row, vm).catch((e) => showError(e.message)); };
    feeNote.textContent = marketPure.externalFeeNote(vm);
    feeNote.hidden = false;
    if (vm.externalUrl) {
      extLink.textContent = `View on ${vm.marketplace} ↗`;
      extLink.hidden = false;
      extLink.onclick = () => window.open(vm.externalUrl, '_blank', 'noopener');
    }
  } else if (vm.external) {
    action.textContent = vm.marketplace ? `Buy on ${vm.marketplace} ↗` : 'External listing';
    action.disabled = !vm.externalUrl;
    action.onclick = () => { if (vm.externalUrl) window.open(vm.externalUrl, '_blank', 'noopener'); };
  } else {
    action.textContent = `Buy — ${vm.priceLabel}`;
    action.disabled = false;
    action.onclick = () => { closeListingDetail(); openBuyFlow(row).catch((e) => showError(e.message)); };
  }
  renderListingHistory([]);
  // #283: bids apply to characters only, and only when the viewer isn't the
  // seller (external listings included — that's the point: act on them here).
  const bidsLine = el('listing-detail-bids');
  const bidBtn = el('listing-detail-bid');
  const bidForm = el('listing-bid-form');
  bidsLine.hidden = true;
  bidForm.hidden = true;
  el('listing-bid-price').value = ''; // never carry a price across listings
  const canBid = vm.kind === 'character' && (!me || !me.wallet || me.wallet !== vm.seller);
  bidBtn.hidden = !canBid;
  bidBtn.onclick = () => { bidForm.hidden = !bidForm.hidden; if (!bidForm.hidden) el('listing-bid-price').focus(); };
  el('listing-bid-confirm').onclick = () => {
    const price = el('listing-bid-price').value.trim();
    const checked = marketPure.validatePrice(price);
    if (!checked.ok) { showError(checked.error); return; }
    placeBid(row, price).catch((e) => showError(e.message));
  };
  el('listing-overlay').hidden = false;
  el('listing-detail-close').focus();
  // History loads after the overlay opens — non-blocking, best-effort.
  try {
    const qs = vm.kind === 'trait'
      ? `slot=${encodeURIComponent(vm.slot)}&value=${encodeURIComponent(vm.value)}`
      : `nft_id=${encodeURIComponent(vm.nftId)}`;
    const data = await api(`/api/market/history?${qs}`);
    if (!el('listing-overlay').hidden && activeListingId === requestId) {
      renderListingHistory(data.events || data.sales || []);
    }
    if (vm.kind === 'character') {
      const bd = await api(`/api/market/bids?nft_id=${encodeURIComponent(vm.nftId)}`);
      if (!el('listing-overlay').hidden && activeListingId === requestId && (bd.bids || []).length) {
        const top = bd.bids[0];
        const bidsLine2 = el('listing-detail-bids');
        bidsLine2.textContent = `Top bid: ${top.amount_xrp} XRP (${bd.bids.length} bid${bd.bids.length > 1 ? 's' : ''})`;
        bidsLine2.hidden = false;
      }
    }
  } catch (e) { /* history is decorative; the overlay stays useful without it */ }
}

const MARKET_PAGE_SIZE = 24;

// #203: append=true fetches the next page ("Load more") and appends; a fresh
// load resets offset. `market-load-more` shows while loaded < total.
async function loadMarketBrowse({ append = false } = {}) {
  highlightTabs('market-kind', 'kind', marketState.kind);
  const grid = el('market-grid');
  if (!append) {
    marketState.offset = 0;
    showGridSkeletons(grid);
  }
  el('market-empty').hidden = true;
  const slot = el('market-trait-slot').value.trim();
  const value = el('market-trait-value').value.trim();
  const traits = slot && value ? [marketPure.traitFilterToken(slot, value)] : [];
  // #239 per-kind denomination: the same min/max inputs filter XRP for
  // characters and BRIX for traits (min_brix/max_brix server params).
  const minPrice = el('market-min-xrp').value.trim();
  const maxPrice = el('market-max-xrp').value.trim();
  const isTrait = marketState.kind === 'trait';
  const pairs = marketPure.buildListingsParams({
    kind: marketState.kind,
    traits,
    minXrp: isTrait ? '' : minPrice,
    maxXrp: isTrait ? '' : maxPrice,
    minBrix: isTrait ? minPrice : '',
    maxBrix: isTrait ? maxPrice : '',
    sort: el('market-sort').value,
    limit: MARKET_PAGE_SIZE,
    offset: append ? marketState.offset : 0,
    // #131/#203: read these controls defensively — a stale cached
    // index.html paired with fresh app.js (Discord webview / browser cache
    // skew) would otherwise throw here, before the try below, and blank the
    // whole grid. Missing element -> the old default behavior.
    includeExternal: Boolean(el('market-include-external')?.checked ?? true),
    seller: el('market-mine-only')?.checked && me && me.wallet ? me.wallet : '',
  });
  const qs = new URLSearchParams();
  for (const [k, v] of pairs) qs.append(k, v);
  try {
    const data = await api(`/api/market/listings?${qs.toString()}`);
    const rows = data.rows || [];
    renderMarketGrid(rows, { append });
    marketState.offset = (append ? marketState.offset : 0) + rows.length;
    const total = data.total ?? marketState.offset;
    el('market-load-more').hidden = marketState.offset >= total;
  } catch (e) {
    if (!append) grid.replaceChildren();
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
    img.loading = 'lazy';
    img.alt = '';
    const label = document.createElement('span');
    label.className = 'trait-chip-label';
    label.textContent = entry.label;
    const btn = document.createElement('button');
    btn.className = 'chip-action';
    btn.textContent = actionLabel;
    // #133: onAction may be async (cancelListing) — same silent-rejection
    // hazard as the browse-grid cards; Promise.resolve covers sync actions.
    btn.onclick = () => Promise.resolve().then(() => onAction(entry.payload)).catch((e) => showError(e.message));
    chip.replaceChildren(img, label, btn);
    containerEl.appendChild(chip);
  }
}

// Best-effort trait art for Mine's unlisted-traits/loose-Closet chips: reuses
// the active Dressing Room character's body (if the economy state happens to
// be loaded already) exactly like renderTraitStrip() does; falls back to no
// image rather than fetching economy state just for a thumbnail.
// The server picks a disk-verified display body for each (slot, value) and
// sends it as image_url — trait art usually is NOT under the active
// character's body (it lives in shared/ or another body), so guessing the body
// client-side produced 404s for most tiles. Fall back to the old body-pinned
// guess only for a row from an older server that sends no image_url.
function mineTraitImgSrc(slot, value, imageUrl) {
  if (imageUrl) return traitLayerSrc(imageUrl);
  if (!economyState) return null;
  const char = activeChar();
  return char && layerComplete(char.body, value) ? layerSrc(char.body, slot, value) : null;
}

function renderMineGroups(data) {
  const listingEntries = data.listings.map((row) => {
    const vm = marketPure.mapListingRow(row);
    return {
      imgSrc: marketRowImgSrc(vm),
      label: `${vm.title} — ${vm.priceLabel}`,
      payload: row,
    };
  });
  renderChipList(el('mine-listings'), el('mine-listings-empty'), listingEntries, 'Cancel', cancelListing);

  const charEntries = data.unlisted_characters.map((c) => {
    const label = c.nft_number != null ? `#${c.nft_number}` : c.nft_id;
    return {
      imgSrc: c.image ? imgUrl(c.image, THUMB_W) : null,
      label,
      payload: { nftId: c.nft_id, label, wizard: false },
    };
  });
  renderChipList(el('mine-characters'), el('mine-characters-empty'), charEntries, 'List', openListForm);

  const traitEntries = data.unlisted_trait_tokens.map((t) => ({
    imgSrc: mineTraitImgSrc(t.slot, t.value, t.image_url),
    label: `${t.slot}: ${t.value}`,
    payload: { nftId: t.nft_id, slot: t.slot, value: t.value, label: `${t.slot}: ${t.value}`, wizard: false },
  }));
  renderChipList(el('mine-traits'), el('mine-traits-empty'), traitEntries, 'List', openListForm);

  const closetEntries = data.closet_assets.map((a) => ({
    imgSrc: mineTraitImgSrc(a.slot, a.value, a.image_url),
    label: `${a.slot}: ${a.value} ×${a.count}`,
    payload: { slot: a.slot, value: a.value, label: `${a.slot}: ${a.value}`, wizard: true },
  }));
  renderChipList(el('mine-closet'), el('mine-closet-empty'), closetEntries, 'Sell', openListForm);
}

function renderBidGroups(data) {
  const myEntries = (data.my_bids || []).map((b) => ({
    imgSrc: b.image ? imgUrl(b.image, THUMB_W) : null,
    label: `${b.nft_number != null ? `#${b.nft_number}` : b.nft_id} — ${b.amount_xrp} XRP`,
    payload: b,
  }));
  renderChipList(el('mine-bids'), el('mine-bids-empty'), myEntries, 'Cancel', cancelBid);
  const inEntries = (data.bids_on_my_nfts || []).map((b) => ({
    imgSrc: b.image ? imgUrl(b.image, THUMB_W) : null,
    label: `${b.nft_number != null ? `#${b.nft_number}` : b.nft_id} — ${b.amount_xrp} XRP`,
    payload: b,
  }));
  renderChipList(el('mine-incoming-bids'), el('mine-incoming-bids-empty'), inEntries, 'Accept', acceptBid);
}

async function loadMarketMine() {
  try {
    const data = await api('/api/market/mine');
    renderMineGroups(data);
  } catch (e) {
    showError(e.message);
  }
  // #283: bids load separately — a bids failure must not blank the listings.
  try {
    renderBidGroups(await api('/api/market/bids/mine'));
  } catch (e) {
    renderBidGroups({});
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
  const gen = ++marketFlowGen;
  // Shared-panel ownership: another flow kind (e.g. a shop buy) taking over
  // flow-panel bumps flowRenderGen but not OUR gen — track it too, refreshed
  // after each render this poller makes itself.
  let ownerGen = flowRenderGen;
  const path = MARKET_STATUS_PATH[kind](sessionId);
  const tick = async () => {
    if (gen !== marketFlowGen || ownerGen !== flowRenderGen) return; // superseded
    if (el('flow-panel').hidden) return; // user navigated away
    let s;
    try {
      s = await api(path);
    } catch (e) {
      if (gen === marketFlowGen && ownerGen === flowRenderGen) {
        marketFlowTimer = setTimeout(tick, 3000); // transient; keep polling
      }
      return;
    }
    if (gen !== marketFlowGen || ownerGen !== flowRenderGen) return; // superseded while we awaited
    showFlow(render(s));
    ownerGen = flowRenderGen; // our own render; keep ownership current
    if (!marketPure.isMarketTerminal(s.state)) marketFlowTimer = setTimeout(tick, 3000);
  };
  marketFlowTimer = setTimeout(tick, 3000);
}

// #221: per-kind render lookup for a RESUMED market session — the same render
// fns marketFlow(...) passes at start time. buy is a per-listing-kind factory,
// hence the indirection through the session dict.
const MARKET_RESUME_RENDER = {
  list: () => marketListRender,
  cancel: () => marketCancelRender,
  buy: (s) => marketBuyRender(s.listing_kind || 'character'),
  bid: () => marketBidRender,
  bid_accept: () => marketBidAcceptRender,
  trait_list: () => marketTraitListRender,
};

// Re-attach to a running marketplace op (#221): render its real state
// immediately from the resumed session dict, then resume the normal poll.
function attachMarketResume(session) {
  const pick = MARKET_RESUME_RENDER[session.kind];
  if (!pick || !MARKET_STATUS_PATH[session.kind]) return false;
  const render = pick(session);
  clearTimeout(marketFlowTimer);
  showPanel('flow-panel');
  showFlow(render(session));
  if (!marketPure.isMarketTerminal(session.state)) {
    pollMarketFlow(session.kind, session.id, render);
  }
  return true;
}

// Re-attach to a running economy op (#221): compact reconnect -> result
// overlay (matches mint's reconnect banner) rather than re-opening the full
// Dressing Room panel; pollEconomyOp drives the same status endpoint the
// original start call would have polled.
const ECONOMY_OP_LABEL = {
  harvest: 'Harvest', assemble: 'Assemble', equip: 'Save',
  extract: 'Extract', deposit: 'Deposit',
};
function attachEconomyResume(session) {
  const kind = session.kind;
  if (!ECONOMY_OP_LABEL[kind]) return false;
  showPanel('flow-panel');
  showFlow({
    title: '🔄 Reconnecting…',
    text: `You have a ${ECONOMY_OP_LABEL[kind]} in progress — picking it back up.`,
    spinner: true,
  });
  // Ownership: showFlow() bumps flowRenderGen on EVERY flow-panel render, so
  // capturing it right after our own render means ANY later takeover of the
  // panel — a normal flow start or another resume attach, not just
  // invalidateFlowPolls — supersedes this pending result callback.
  const gen = flowRenderGen;
  pollEconomyOp(kind, session).then((final) => {
    if (gen !== flowRenderGen) return; // another flow owns the panel now
    if (el('flow-panel').hidden) return; // user navigated away
    if (final.state === 'failed') {
      showFlow({ title: `❌ ${ECONOMY_OP_LABEL[kind]} failed`, text: final.error || 'Something went wrong.', done: true });
    } else if (final.accept) {
      // harvest (legacy upgrade), assemble and extract can end with an offer
      // the user must still accept in Xaman — surface its QR/deep link.
      showFlow({
        title: '👛 Accept in Xaman',
        text: signText(final.accept_push, 'Scan to accept in Xaman.'),
        qrData: final.accept,
        link: final.accept,
        push: final.accept_push,
        done: true,
      });
    } else {
      showFlow({
        title: `✅ ${ECONOMY_OP_LABEL[kind]} complete`,
        text: 'Open the Dressing Room to see the result.',
        done: true,
      });
    }
  });
  return true;
}

async function marketFlow(kind, startPath, body, render) {
  clearTimeout(marketFlowTimer);
  ++marketFlowGen; // kill any in-flight tick across the awaited start POST
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
    if (e.body && e.body.code === 'trustline_required') {
      // #441: a trait buy needs a BRIX line to receive the on-ramped BRIX;
      // set it, then re-issue the very same buy so the user lands back on
      // the listing they chose. Backing out (no line) just returns to browse.
      startBrixTrustline({
        back: () => showPanel('market-panel'),
        onSet: () => marketFlow(kind, startPath, body, render),
      });
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
    return { title: '📋 List for sale', text: signText(s.push, 'Scan to sign the sell offer in Xaman.'), qrData: s.xumm_url, link: s.xumm_url, push: s.push };
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
    return { title: '🗑️ Cancel listing', text: signText(s.push, 'Scan to sign the cancellation in Xaman.'), qrData: s.xumm_url, link: s.xumm_url, push: s.push };
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
      return { title: '💳 Confirm purchase', text: signText(s.push, s.instruction || 'Scan to sign the purchase in Xaman.'), qrData: s.xumm_url, link: s.xumm_url, push: s.push };
    }
    // #239 two-step on-ramp: sign the XRP→BRIX top-up first, then the accept.
    if (s.state === 'awaiting_onramp') {
      const quote = s.price_xrp_quote ? ` (~${s.price_xrp_quote} XRP)` : '';
      return { title: `💱 Get BRIX${quote}`, text: signText(s.push, s.instruction || 'Scan to buy the BRIX for this purchase in Xaman.'), qrData: s.xumm_url, link: s.xumm_url, push: s.push };
    }
    if (s.state === 'onramp_confirmed') {
      return { title: '⏳ BRIX acquired', text: 'Preparing your purchase…', spinner: true };
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
    return { title: `🎟️ ${step}`, text: signText(s.extract_push, 'Scan to accept your trait token in Xaman.'), qrData: s.extract_xumm_url, link: s.extract_xumm_url, push: s.extract_push };
  }
  if (s.state === 'list_pending') {
    return { title: `📤 ${step}`, text: signText(s.list_push, 'Scan to sign the sell offer in Xaman.'), qrData: s.list_xumm_url, link: s.list_xumm_url, push: s.list_push };
  }
  if (s.state === 'listed') {
    return { title: '🎉 Listed!', text: 'Your trait is live on the Marketplace.', done: true };
  }
  return { title: '❌ Sell failed', text: s.error || 'Something went wrong.', done: true };
}

// --- Trait Shop (#217): catalog grid + reuses marketFlow's overlay pieces
// (showPanel/showFlow/promptClosetRequired) but drives its own POST/GET pair
// since ShopBuySession's shape (accept is a nested payload dict, not a flat
// xumm_url) differs from the market sessions' MARKET_STATUS_PATH table.

let shopFlowTimer = null;
let shopFlowGen = 0; // see marketFlowGen

function shopImgSrc(item) {
  return traitLayerSrc(item.image_url);
}

function renderShopGrid(items) {
  const grid = el('shop-grid');
  const empty = el('shop-empty');
  grid.replaceChildren();
  if (!items.length) { empty.hidden = false; return; }
  empty.hidden = true;
  for (const item of items) {
    const card = document.createElement('button');
    card.className = 'nft-card';
    const img = document.createElement('img');
    img.src = shopImgSrc(item) || BLANK_IMG;
    img.loading = 'lazy';
    img.alt = '';
    const name = document.createElement('span');
    name.className = 'cap';
    name.textContent = `${item.slot}: ${item.value}`;
    const price = document.createElement('span');
    price.className = 'market-card-price';
    price.textContent = `${item.price_brix} BRIX`;
    name.appendChild(price);
    card.replaceChildren(img, name);
    card.onclick = () => openShopBuyFlow(item).catch((e) => showError(e.message));
    grid.appendChild(card);
  }
}

// #217 follow-up: Shop filter state — the catalog arrives whole from the
// single cached endpoint, so slot/search/sort are pure client-side re-renders.
const shopState = { items: [], slot: 'all', query: '', sort: 'price_asc' };

function renderShopChips() {
  const bar = el('shop-slot-chips');
  bar.replaceChildren();
  for (const { slot, count } of marketPure.shopSlotCounts(shopState.items)) {
    const chip = document.createElement('button');
    chip.className = 'lb-chip';
    chip.dataset.slot = slot;
    chip.setAttribute('role', 'tab');
    chip.textContent = slot === 'all' ? `All · ${count}` : `${slot} · ${count}`;
    chip.classList.toggle('active', shopState.slot === slot);
    chip.setAttribute('aria-selected', String(shopState.slot === slot));
    chip.onclick = () => {
      shopState.slot = slot;
      highlightTabs('shop-slot-chips', 'slot', slot);
      applyShopFilters();
    };
    bar.appendChild(chip);
  }
}

function applyShopFilters() {
  renderShopGrid(marketPure.filterShopItems(shopState.items, shopState));
}

// Trait Shop (#217) visibility. With SHOP_ENABLED off the project does not
// sell traits — users only buy them from each other — so the Shop tab is
// removed rather than left over a permanently empty catalog. The API is the
// real gate (catalog returns [], buy returns 403 shop_disabled); this is
// presentation only.
//
// Fail-closed: the tab ships hidden in index.html and this flag starts false,
// so a slow or failed /api/config never routes users to a surface the server
// will refuse to serve. Only an explicit shop_enabled:true reveals it.
let shopEnabled = false;

function applyShopVisibility(cfg) {
  shopEnabled = cfg.shop_enabled === true;
  const chip = document.querySelector('#market-tabs [data-tab="shop"]');
  if (chip) chip.hidden = !shopEnabled;
  if (!shopEnabled) {
    const section = el('market-shop');
    if (section) section.hidden = true;
  }
}

async function loadShopCatalog() {
  const grid = el('shop-grid');
  showGridSkeletons(grid);
  el('shop-empty').hidden = true;
  try {
    const data = await api('/api/shop/catalog');
    shopState.items = data.items || [];
    renderShopChips();
    applyShopFilters();
  } catch (e) {
    grid.replaceChildren();
    showError(e.message);
  }
}

function shopBuyRender(s) {
  if (s.state === 'settling') {
    return { title: '⏳ Settling', text: 'Adding your trait to the Closet…', spinner: true };
  }
  if (s.state === 'done') {
    return { title: '🎉 Purchase complete!', text: 'Added to your Closet.', done: true };
  }
  if (s.state === 'awaiting_accept') {
    const url = s.accept ? s.accept.xumm_url : null;
    // #238: silent payment-path fallback — no BRIX? The offer is priced in XRP.
    const price = s.pay_with === 'XRP' && s.price_xrp
      ? `~${s.price_xrp} XRP`
      : `${s.price_brix} BRIX`;
    return { title: '💳 Confirm purchase', text: `Scan to accept the trait offer in Xaman (${price}).`, qrData: url, link: url };
  }
  if (s.state === 'failed') {
    return { title: '❌ Purchase failed', text: s.error || 'Something went wrong.', done: true };
  }
  return { title: '⏳ Preparing…', text: 'Minting your trait…', spinner: true };
}

function pollShopFlow(sessionId) {
  clearTimeout(shopFlowTimer);
  const gen = ++shopFlowGen;
  let ownerGen = flowRenderGen; // see pollMarketFlow
  const path = `/api/shop/buy/${sessionId}`;
  const tick = async () => {
    if (gen !== shopFlowGen || ownerGen !== flowRenderGen) return; // superseded
    if (el('flow-panel').hidden) return; // user navigated away
    let s;
    try {
      s = await api(path);
    } catch (e) {
      if (gen === shopFlowGen && ownerGen === flowRenderGen) {
        shopFlowTimer = setTimeout(tick, 3000); // transient; keep polling
      }
      return;
    }
    if (gen !== shopFlowGen || ownerGen !== flowRenderGen) return; // superseded while we awaited
    showFlow(shopBuyRender(s));
    ownerGen = flowRenderGen; // our own render; keep ownership current
    if (!marketPure.isMarketTerminal(s.state)) shopFlowTimer = setTimeout(tick, 3000);
  };
  shopFlowTimer = setTimeout(tick, 3000);
}

async function resumeShopBuy(sessionId) {
  showPanel('flow-panel');
  showFlow({ title: 'Resuming…', spinner: true });
  let s;
  try {
    s = await api(`/api/shop/buy/${sessionId}`);
  } catch (e) {
    showFlow({ title: '❌ Could not resume', text: e.message, done: true });
    return;
  }
  showFlow(shopBuyRender(s));
  if (!marketPure.isMarketTerminal(s.state)) pollShopFlow(sessionId);
}

async function openShopBuyFlow(item) {
  const ok = await confirmDialog({
    title: `Buy ${item.slot}: ${item.value}?`,
    text: `${item.price_brix} BRIX will be spent.`,
    confirmLabel: 'Buy now',
  });
  if (!ok) return;
  clearTimeout(shopFlowTimer);
  ++shopFlowGen; // kill any in-flight tick across the awaited start POST
  showPanel('flow-panel');
  showFlow({ title: 'Starting…', spinner: true });
  let s;
  try {
    s = await api('/api/shop/buy', {
      method: 'POST',
      body: JSON.stringify({ slot: item.slot, value: item.value }),
    });
  } catch (e) {
    if (e.message === 'closet_required') {
      showPanel('market-panel');
      promptClosetRequired();
      return;
    }
    // 409 session_active: resume the caller's already-running purchase
    // rather than erroring opaquely — the endpoint returns session_id.
    if (e.body && e.body.code === 'session_active' && e.body.session_id) {
      await resumeShopBuy(e.body.session_id);
      return;
    }
    showFlow({ title: '❌ Could not start', text: e.message, done: true });
    return;
  }
  showFlow(shopBuyRender(s));
  if (!marketPure.isMarketTerminal(s.state)) pollShopFlow(s.id);
}

// --- #283: native bids ---

function marketBidRender(s) {
  if (s.state === 'pending') {
    return { title: '⏳ Confirming', text: 'Signature received — waiting for the ledger to confirm…', spinner: true };
  }
  if (s.state === 'done') {
    return { title: '🎉 Bid placed!', text: 'Your bid is live on-ledger. The owner can accept it any time before it expires (7 days).', done: true };
  }
  if (s.state === 'awaiting_signature') {
    return { title: '🏷️ Place bid', text: signText(s.push, 'Scan to sign your bid in Xaman.'), qrData: s.xumm_url, link: s.xumm_url, push: s.push };
  }
  if (s.state === 'unknown') {
    return { title: "⏳ Couldn't confirm", text: "We couldn't confirm the bid in time — check My bids shortly; it may still have gone through.", done: true };
  }
  return { title: '❌ Bid failed', text: s.error || 'Something went wrong.', done: true };
}

function marketBidAcceptRender(s) {
  if (s.state === 'pending') {
    return { title: '⏳ Confirming', text: 'Signature received — waiting for the ledger to confirm…', spinner: true };
  }
  if (s.state === 'done') {
    return { title: '🎉 Sold!', text: 'You accepted the bid — the XRP is in your wallet and the NFT is on its way to the bidder.', done: true };
  }
  if (s.state === 'awaiting_signature') {
    return { title: '🤝 Accept bid', text: signText(s.push, 'Scan to accept the bid in Xaman.'), qrData: s.xumm_url, link: s.xumm_url, push: s.push };
  }
  if (s.state === 'unknown') {
    return { title: "⏳ Couldn't confirm", text: "We couldn't confirm the accept in time — it may still have gone through.", done: true };
  }
  const why = s.reason === 'bid_unavailable' ? 'The bid expired, was cancelled, or the bidder no longer has the funds.' : (s.error || 'Something went wrong.');
  return { title: '❌ Accept failed', text: why, done: true };
}

async function placeBid(row, priceXrp) {
  closeListingDetail();
  await marketFlow('bid', '/api/market/bid', { nft_id: row.nft_id, price_xrp: priceXrp }, marketBidRender);
}

// --- #426: "Buy now" on an external (brokered) listing ---
//
// The seller's offer is destination-locked to the broker, so we can't settle
// it — but the broker's bot auto-settles any plain buy offer that clears the
// ask after its fee, wherever it came from. Buy now is therefore the EXISTING
// bid flow at the server-computed clearing price (clearing_xrp on the row);
// no new session type, no new endpoint. What differs is the copy after the
// bid lands: settlement is somebody else's bot, so we keep polling the bid's
// status (`fill` from the index) and only say "yours" once the ledger shows
// the offer consumed; after ~5 minutes unfilled we say so honestly.

const EXTERNAL_FILL_WAIT_MS = 5 * 60 * 1000;
const EXTERNAL_FILL_POLL_MS = 5000;
let externalFillTimer = null;

async function buyExternalNow(row, vm) {
  const ok = await confirmDialog({
    title: `Buy now via ${vm.marketplace || 'the marketplace'}?`,
    text: `${vm.clearingXrp} XRP — ${marketPure.externalFeeNote(vm)} Your offer expires on its own if it isn't taken (nothing is held).`,
    confirmLabel: 'Place offer',
  });
  if (!ok) return;
  closeListingDetail();
  await marketFlow(
    'bid',
    '/api/market/bid',
    { nft_id: row.nft_id, price_xrp: vm.clearingXrp },
    marketExternalBuyRender(vm.marketplace),
  );
}

function marketExternalBuyRender(marketplace) {
  return (s) => {
    if (s.state === 'done') {
      const copy = marketPure.externalFillCopy(marketplace, s.fill);
      // Start the fill watch AFTER showFlow() has painted this render (it
      // bumps flowRenderGen; a watch armed before it would see itself as
      // superseded and stop).
      if (!copy.done) setTimeout(() => watchExternalFill(s.id, marketplace, Date.now()), 0);
      return { ...copy, spinner: !copy.done };
    }
    const base = marketBidRender(s);
    if (s.state === 'awaiting_signature') {
      return { ...base, title: '🛒 Buy now', text: signText(s.push, `Scan to sign your ${marketplace || 'marketplace'} offer in Xaman.`) };
    }
    return base;
  };
}

function watchExternalFill(sessionId, marketplace, startedAt) {
  clearTimeout(externalFillTimer);
  const gen = marketFlowGen;
  const ownerGen = flowRenderGen;
  externalFillTimer = setTimeout(async () => {
    if (gen !== marketFlowGen || ownerGen !== flowRenderGen) return; // another flow took the panel
    if (el('flow-panel').hidden) return; // user navigated away
    let s;
    try {
      s = await api(MARKET_STATUS_PATH.bid(sessionId));
    } catch (e) {
      if (gen === marketFlowGen && ownerGen === flowRenderGen) watchExternalFill(sessionId, marketplace, startedAt);
      return;
    }
    if (gen !== marketFlowGen || ownerGen !== flowRenderGen) return;
    const unfilled = s.fill == null || s.fill === 'live';
    const fill = unfilled && (Date.now() - startedAt) >= EXTERNAL_FILL_WAIT_MS ? 'waiting' : s.fill;
    const copy = marketPure.externalFillCopy(marketplace, fill);
    showFlow({ ...copy, spinner: !copy.done });
    if (!copy.done) watchExternalFill(sessionId, marketplace, startedAt);
  }, EXTERNAL_FILL_POLL_MS);
}

async function cancelBid(bid) {
  const ok = await confirmDialog({
    title: 'Cancel this bid?',
    text: `Your ${bid.amount_xrp} XRP bid will be withdrawn.`,
    confirmLabel: 'Cancel bid',
  });
  if (!ok) return;
  await marketFlow('cancel', '/api/market/cancel', { offer_index: bid.offer_index }, marketCancelRender);
}

async function acceptBid(bid) {
  // Net amount computed from marketPure's fee constants (single source of
  // truth for the 93/7 split) rather than a hand-written figure.
  const priced = marketPure.safeComputeRoyalty(bid.amount_xrp);
  const netText = priced.ok
    ? `you net ${priced.royalty.receiveXrp} XRP (93% — 7% collection royalty)`
    : 'you net 93% (7% collection royalty)';
  const ok = await confirmDialog({
    title: 'Accept this bid?',
    text: `Sell for ${bid.amount_xrp} XRP — ${netText}.`,
    confirmLabel: 'Accept bid',
  });
  if (!ok) return;
  await marketFlow('bid_accept', '/api/market/bid/accept', { offer_index: bid.offer_index }, marketBidAcceptRender);
}

async function openBuyFlow(row) {
  const vm = marketPure.mapListingRow(row);
  let text;
  if (vm.amountBrix != null) {
    // #239: trait listings are BRIX-denominated; the on-ramp (if needed) is
    // quoted by the server once the buy starts.
    text = `${vm.amountBrix} BRIX — seller nets 93% (7% collection royalty). No BRIX? You'll get a one-tap XRP top-up first.`;
  } else {
    // #133: a malformed server-provided price would make computeRoyalty throw
    // and the confirm dialog never open — surface it instead of a dead click.
    const priced = marketPure.safeComputeRoyalty(vm.amountXrp);
    if (!priced.ok) {
      showError(`This listing has an invalid price (${priced.error}) — try refreshing.`);
      return;
    }
    text = `${vm.amountXrp} XRP — seller nets ${priced.royalty.receiveXrp} XRP (93% — 7% collection royalty).`;
  }
  const ok = await confirmDialog({
    title: `Buy ${vm.title}?`,
    text,
    confirmLabel: 'Buy now',
  });
  if (!ok) return;
  await marketFlow('buy', '/api/market/buy', { offer_index: row.offer_index }, marketBuyRender(row.kind));
}

async function cancelListing(row) {
  const vm = marketPure.mapListingRow(row);
  const ok = await confirmDialog({
    title: 'Cancel this listing?',
    text: `${vm.title} — ${vm.priceLabel} will no longer be for sale.`,
    confirmLabel: 'Cancel listing',
  });
  if (!ok) return;
  await marketFlow('cancel', '/api/market/cancel', { offer_index: row.offer_index }, marketCancelRender);
}

// #239: a wizard (Closet) item or a loose trait token lists in BRIX;
// characters list in XRP.
function listFormIsTrait(item) {
  return Boolean(item && (item.wizard || item.slot));
}

function openListForm(item) {
  marketPendingItem = item;
  showPanel('market-list-form-panel');
  el('market-list-form-title').textContent = item.wizard ? 'Sell a trait' : 'List for sale';
  el('market-list-form-sub').textContent = item.label;
  el('market-list-price').value = '';
  el('market-list-price').placeholder = listFormIsTrait(item) ? 'Price in BRIX' : 'Price in XRP';
  el('market-list-royalty').hidden = true;
}

function updateListFormRoyaltyPreview() {
  const out = el('market-list-royalty');
  const raw = el('market-list-price').value.trim();
  const isTrait = listFormIsTrait(marketPendingItem);
  const check = isTrait ? marketPure.validateBrixPrice(raw) : marketPure.validatePrice(raw);
  if (check.ok) {
    out.hidden = false;
    out.textContent = isTrait
      ? marketPure.brixRoyaltyDisclosure(raw)
      : marketPure.royaltyDisclosure(raw);
  } else {
    out.hidden = true;
  }
}

async function submitListForm() {
  const item = marketPendingItem;
  if (!item) return;
  const price = el('market-list-price').value.trim();
  const isTrait = listFormIsTrait(item);
  const check = isTrait ? marketPure.validateBrixPrice(price) : marketPure.validatePrice(price);
  if (!check.ok) { showError(check.error); return; }
  const ok = await confirmDialog({
    title: item.wizard ? 'Post this trait for sale?' : 'List for sale?',
    text: isTrait ? marketPure.brixRoyaltyDisclosure(price) : marketPure.royaltyDisclosure(price),
    confirmLabel: item.wizard ? 'Post listing' : 'List it',
  });
  if (!ok) return;
  if (item.wizard) {
    await marketFlow(
      'trait_list', '/api/market/trait/list',
      { slot: item.slot, value: item.value, price_brix: price },
      marketTraitListRender,
    );
  } else if (isTrait) {
    await marketFlow('list', '/api/market/list', { nft_id: item.nftId, price_brix: price }, marketListRender);
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
  // Referral stash (#41 follow-on): a share click-through arrives as
  // ?ref=<wallet>. Persist it for the future mint-attribution flow; shape-
  // check so arbitrary query junk never lands in storage.
  try {
    const refParam = new URLSearchParams(location.search).get('ref');
    if (refParam && XRPL_ADDR_RE.test(refParam)) {
      localStorage.setItem('lfg_ref', JSON.stringify({ ref: refParam, ts: Date.now() }));
    }
  } catch (_) { /* private mode / no storage */ }

  setupLogo();
  setupLeaderboard();
  setupBrixCard();
  el('register-retry-btn').onclick = () => (insideWeb ? startWebSignin() : startSignin());
  el('mint-btn').onclick = () => startMint();
  el('flow-regen-btn').onclick = onFlowRegen;
  el('swap-btn').onclick = () => openDressup();
  el('dressup-back-btn').onclick = async () => {
    if (!(await confirmDiscardIfDirty())) return;
    showMintHome();
  };
  const saveBtn = el('build-save-btn');
  if (saveBtn) saveBtn.onclick = () => saveBuild();
  const discardBtn = el('build-discard-btn');
  if (discardBtn) discardBtn.onclick = () => discardPending();
  el('go-switch-btn').onclick = () => openGoPicker();
  el('swapper-btn').onclick = () => openSwapper();
  el('swap-back-btn').onclick = () => showMintHome();
  el('pick-traits-btn').onclick = showTraitChooser;
  el('swap-cancel-btn').onclick = () => openSwapper();
  el('swap-confirm-btn').onclick = confirmSwap;
  el('swap-done-btn').onclick = () => showMintHome();
  el('change-wallet-btn').onclick = async () => {
    await wcSignOut(); // #447: switching wallets must drop the Joey pairing too
    return insideWeb ? startWebSignin() : startSignin();
  };
  // #447 — null-guarded: a cached older index.html has none of these nodes.
  const wcBtn = el('register-wc-btn');
  if (wcBtn) wcBtn.onclick = () => startWcSignin();
  const linkBtn = el('link-wallet-btn');
  if (linkBtn) linkBtn.onclick = () => startLinkWallet();
  const linkJoey = el('link-joey-btn');
  if (linkJoey) linkJoey.onclick = () => startLinkJoey();
  const linkXaman = el('link-xaman-btn');
  if (linkXaman) linkXaman.onclick = () => startLinkXaman();
  const linkBack = el('link-back-btn');
  if (linkBack) linkBack.onclick = () => { clearTimeout(linkPollTimer); linkPollGen++; showMintHome(); };
  el('flow-done-btn').onclick = () => { showMintHome(); };
  el('bulk-done-btn').onclick = () => { clearTimeout(bulkPollTimer); bulkPollGen++; currentBulkId = null; showMintHome(); };
  el('offers-btn').onclick = () => openOffers();
  el('offers-back-btn').onclick = () => { refreshOffersBadge(true); showMintHome(); };

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
  el('market-include-external').onchange = () => loadMarketBrowse();
  el('market-mine-only').onchange = () => loadMarketBrowse();
  el('shop-search').oninput = () => { shopState.query = el('shop-search').value; applyShopFilters(); };
  el('shop-sort').onchange = () => { shopState.sort = el('shop-sort').value; applyShopFilters(); };
  el('market-load-more').onclick = () => loadMarketBrowse({ append: true });
  el('listing-detail-close').onclick = closeListingDetail;
  el('listing-overlay').onclick = (e) => { if (e.target === el('listing-overlay')) closeListingDetail(); };
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !el('listing-overlay').hidden) closeListingDetail();
  });
  el('market-list-price').addEventListener('input', updateListFormRoyaltyPreview);
  el('market-list-confirm-btn').onclick = submitListForm;
  el('market-list-cancel-btn').onclick = () => showPanel('market-panel');

  // Dev live-reload: runs even in degraded mode (no frame_id).
  try {
    const cfg = await api('/api/config');
    appCfg = cfg;
    // Closet / trait economy ships after the mainnet MVP: with the feature
    // off, hide the Build entry point (the API answers 403 regardless).
    if (cfg.economy_enabled === false) el('swap-btn').hidden = true;
    // In-app marketplace (#44) ships after the mainnet MVP: with the feature
    // off, hide the Marketplace entry point (the API answers 403 regardless).
    if (cfg.market_enabled === false) el('market-btn').hidden = true;
    setupBulkStepper(cfg);
    applyShopVisibility(cfg);
    applyShareConfig(cfg);
    applyWcVisibility();
    // Dev reload is same-origin only — never against a cross-origin API base.
    if (cfg.dev_mode && !API_BASE && 'EventSource' in window) {
      new EventSource('/__dev/reload').onmessage = () => location.reload();
    }
  } catch (_) { /* non-dev or offline: ignore */ }

  // Standalone web surface: the Xaman sign-in IS the auth handshake.
  if (insideWeb) {
    try {
      const user = await setupWeb();
      if (user) {
        me = user;
        // Re-attach to a mint an earlier tab/reload orphaned before going home.
        if (!(await resumeAnyFlow())) showMintHome();
      }
      // else: startWebSignin() is already driving the register panel.
    } catch (e) {
      console.error(e);
      status(`Failed to connect: ${e.message}`);
    }
    return;
  }

  if (!insideTelegram && !insideDiscord) {
    status('Open this inside Telegram or Discord. (Dev mode: API calls will be unauthorized.)');
    return;
  }

  try {
    // Same UI either way — only the auth handshake differs by host.
    if (insideTelegram) await setupTelegram();
    else await setupDiscord();
    me = await api('/api/me');
    if (me.wallet) {
      // Re-attach to a mint the webview reload orphaned before going home.
      if (!(await resumeAnyFlow())) showMintHome();
    }
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
