// Pure decisions for the XRPL Action mint entry point. Keeping URL and state
// handling out of app.js makes the security-sensitive routing contract easy
// to exercise without a browser.

const TERMINAL = new Set([
  'done',
  'rejected',
  'expired',
  'failed',
  'indeterminate',
]);

const ERRORS = Object.freeze({
  action_disabled: 'Atomic minting is not enabled yet.',
  batch_unavailable: 'The connected XRPL network is not ready for atomic Batch minting.',
  obsolete_batch_enabled: 'The connected XRPL network is advertising an unsafe obsolete Batch amendment.',
  mint_offer_unavailable: 'The connected XRPL network cannot create the destination-locked mint offer yet.',
  ticket_unavailable: 'No issuer ticket is available right now. Please try again shortly.',
  capacity_reached: 'The collection is currently at mint capacity.',
  signing_unavailable: 'Xaman could not create the signing request. No payment was taken.',
  storage_unavailable: 'The mint could not be saved safely. No payment was taken.',
  rate_limited: 'Too many mint requests were started. Please wait a minute and try again.',
  rejected: 'The Xaman request was rejected. No payment was taken.',
  expired: 'The atomic mint request expired. No payment was taken.',
  wallet_mismatch: 'The connected wallet does not match this mint request.',
  batch_failed: 'The atomic Batch did not validate. No payment was taken.',
  outcome_indeterminate: 'The ledger outcome needs operator review. Do not submit another copy of this request.',
  record_recovery_required: 'Your NFT reached your wallet, but its local mint record needs repair.',
});

export function requestedAction(search) {
  const action = new URLSearchParams(search || '').get('action');
  return action === 'mint' ? action : null;
}

export function actionIsTerminal(state) {
  return TERMINAL.has(state);
}

export function actionErrorCopy(code) {
  return ERRORS[code] || 'The atomic mint did not complete. No payment was taken.';
}

export function activeActionSessionId(response) {
  const session = response && response.session;
  if (!session || !session.sessionId || actionIsTerminal(session.state)) return null;
  return session.sessionId;
}
