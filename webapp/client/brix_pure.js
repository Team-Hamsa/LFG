// webapp/client/brix_pure.js
// Pure decision logic for the home-screen BRIX card (issue #48, PR-3), kept
// free of DOM/network code so it can be executed and unit-tested under Node
// (tests/test_brix_pure_js.py) — same split as mint_pure.js / market_pure.js.

const NUM = new Intl.NumberFormat();

function plural(n) {
  return `${NUM.format(n)} BRIX`;
}

// Render model for the card, from the GET /api/brix body.
//
//   status — the response body, or null when the fetch itself failed.
//
// Returns { visible, claimable, headline, sub, inFlight, pollClaimId,
//           button: { label, disabled } }.
//
// The card stays HIDDEN until the drip has actually produced something: the
// accrual cron is an ops step that lands after the code, and a permanent
// "0 BRIX" tile would advertise a payout the deployment cannot make yet. A
// wallet with accrual history is always shown even if the meta row is
// missing — that balance is real money and must never be hidden.
// Hidden cards still answer every field claimBrix() reads, so a hidden view
// can never be mistaken for a claimable one (`undefined <= 0` is false).
const HIDDEN = Object.freeze({
  visible: false,
  claimable: 0,
  headline: '',
  sub: '',
  inFlight: false,
  pollClaimId: null,
  button: Object.freeze({ label: 'Nothing to claim', disabled: true }),
});

export function brixCardView(status) {
  if (!status) return HIDDEN;

  const claimable = Number(status.claimable || 0);
  const accrued = Number(status.accrued_total || 0);
  const claimed = Number(status.claimed_total || 0);
  const earning = Number(status.unlisted_last_epoch || 0);
  const open = status.open_claim || null;

  if (!status.last_epoch && accrued === 0 && claimed === 0 && !open) {
    return HIDDEN;
  }

  // An open claim (pending/submitted) has the accruals BOUND to it. Offering
  // a second claim would only earn a 409 claim_in_flight, so the button is
  // disabled and the card polls the claim to completion instead.
  const inFlight = Boolean(open);

  let sub;
  if (inFlight) {
    sub = 'Your claim is on its way — this settles on the ledger in a few seconds.';
  } else if (earning > 0) {
    sub = `${NUM.format(earning)} unlisted NFT${earning === 1 ? '' : 's'} earned yesterday. Listed NFTs don't earn.`;
  } else if (claimable > 0) {
    sub = 'Claim sends the BRIX straight to your wallet.';
  } else {
    sub = "Nothing earned last epoch — listed NFTs don't earn. Hold an unlisted NFT to start the drip.";
  }
  if (!inFlight && claimed > 0) {
    sub += ` Claimed so far: ${plural(claimed)}.`;
  }

  let label;
  if (inFlight) label = 'Claiming…';
  else if (claimable > 0) label = `Claim ${plural(claimable)}`;
  else label = 'Nothing to claim';

  return {
    visible: true,
    claimable,
    headline: plural(claimable),
    sub,
    inFlight,
    pollClaimId: open ? open.claim_id : null,
    button: { label, disabled: inFlight || claimable <= 0 },
  };
}

// How to react to a failed POST /api/brix/claim, by the server's error code.
//
// Returns { message, retryable, refresh, trustline, lockLabel }.
//   retryable — safe to offer the button again immediately. TRUE only when
//               the server bound nothing, so a retry cannot double-claim.
//   refresh   — re-fetch GET /api/brix; the client's picture is stale.
//   trustline — the user needs a BRIX trustline before this can work.
//   lockLabel — non-null ONLY for the server-side preconditions GET /api/brix
//               cannot express (claims_disabled, trustline_required): the
//               button label to pin, disabled, until the next home landing,
//               since the refreshed status still shows the positive balance
//               and would otherwise re-enable "Claim N BRIX" right under a
//               toast saying not to retry. Every `refresh` code is null — its
//               truth IS the refreshed status (an open claim disables + polls;
//               a balance restored by a concurrent claim failing must become
//               claimable again without leaving the home screen).
export function claimErrorView(code) {
  const v = claimErrorBase(code);
  if (!('lockLabel' in v)) v.lockLabel = null;
  return v;
}

function claimErrorBase(code) {
  switch (code) {
    case 'claims_disabled':
      return {
        message: "Claiming isn't open yet — your BRIX keeps accruing in the meantime.",
        retryable: false,
        refresh: false,
        trustline: false,
        lockLabel: 'Claims not open yet',
      };
    case 'trustline_required':
      // #441: the lock is ACTIONABLE — app.js keeps the button enabled under
      // this label and routes the click into the TrustSet flow, so the user
      // never has to add the line by hand in Xaman.
      return {
        message: 'You need a BRIX trustline before you can be paid — one tap sets it up.',
        retryable: false,
        refresh: false,
        trustline: true,
        lockLabel: 'Set BRIX trustline',
      };
    case 'nothing_to_claim':
      return {
        message: 'Nothing to claim right now.',
        retryable: false,
        refresh: true,
        trustline: false,
      };
    case 'claim_in_flight':
      return {
        message: 'A claim is already on its way.',
        retryable: false,
        refresh: true,
        trustline: false,
      };
    case 'claim_unavailable':
      // Nothing was bound server-side (the claim was released, or never
      // opened), so retrying is both safe and the right thing to do.
      return {
        message: "The ledger couldn't be reached. Try again in a minute — nothing was claimed.",
        retryable: true,
        refresh: true,
        trustline: false,
      };
    case 'claim_unconfirmed':
      // The payout MAY have landed. The server deliberately left the balance
      // bound; recovery resolves it from the chain. Retrying here is how a
      // holder gets paid twice, so the button stays off.
      return {
        message:
          "Your claim was submitted but isn't confirmed yet. Don't retry — it'll settle on its own.",
        retryable: false,
        refresh: true,
        trustline: false,
      };
    default:
      return {
        message: 'That claim could not be completed.',
        retryable: false,
        refresh: true,
        trustline: false,
      };
  }
}

// Claim states that end the status poll. Anything else — including an
// unrecognised or missing state — keeps polling: treating an unknown state as
// terminal would strand a live claim mid-flight in the UI.
export function isClaimTerminal(state) {
  return state === 'confirmed' || state === 'failed';
}

// --- BRIX trustline flow (#441) -----------------------------------------
//
// How the trustline sign panel reacts to one POST/GET /api/brix/trustline
// state. Returns { sub, spinner, retry, terminal, clearLock }:
//   terminal  — stop polling.
//   clearLock — the line now exists (signed, or was already set): drop the
//               trustline_required lock so the Claim button re-arms.
//   retry     — offer "Try again" (expired / rejected). The lock stays: the
//               line is still missing.
// Unknown states keep polling — treating one as terminal would strand a live
// sign request, same rule as isClaimTerminal.
export function trustlineView(state) {
  switch (state) {
    case 'signed':
      return { sub: 'BRIX trustline set — you can claim now.', spinner: false, retry: false, terminal: true, clearLock: true };
    case 'already_set':
      return { sub: 'Your wallet already has a BRIX trustline.', spinner: false, retry: false, terminal: true, clearLock: true };
    case 'expired':
      return { sub: 'The trustline request expired.', spinner: false, retry: true, terminal: true, clearLock: false };
    case 'rejected':
      return { sub: 'That was signed by a different wallet — sign it with the wallet you registered.', spinner: false, retry: true, terminal: true, clearLock: false };
    case 'opened':
      return { sub: 'QR scanned — approve the trustline in Xaman…', spinner: true, retry: false, terminal: false, clearLock: false };
    default:
      return { sub: 'Approve the BRIX trustline in Xaman to get paid.', spinner: false, retry: false, terminal: false, clearLock: false };
  }
}

export function isTrustlineTerminal(state) {
  return trustlineView(state).terminal;
}
