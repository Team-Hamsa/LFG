// webapp/client/harvest_pure.js
// Batch-harvest (#356) selection + summary decision logic. Pure, DOM-free and
// Node-testable (tests/test_harvest_pure_js.py) — same pattern as
// mint_pure.js / build_pure.js.

// Toggle nftId in/out of the selection. Pure: returns a new array.
export function toggleSelected(selectedIds, nftId) {
  const cur = Array.isArray(selectedIds) ? selectedIds : [];
  return cur.includes(nftId) ? cur.filter((id) => id !== nftId) : [...cur, nftId];
}

// A tile is batch-selectable only when the single-harvest button would be:
// indexed (body metadata present), not already blank, not already in flight.
export function harvestSelectable(char, harvestingIds) {
  if (!char || !char.body || char.blank) return false;
  const inflight = Array.isArray(harvestingIds) ? harvestingIds : [...(harvestingIds || [])];
  return !inflight.includes(char.nft_id);
}

// {count, mutable, legacy} for the confirm dialog. Legacy (non-mutable)
// units each cost one Xaman accept; mutable in-place strips are free.
export function batchSummary(characters, selectedIds) {
  const byId = new Map((characters || []).map((c) => [c.nft_id, c]));
  let mutable = 0;
  let legacy = 0;
  for (const id of selectedIds || []) {
    const c = byId.get(id);
    if (!c) continue;
    if (c.mutable) mutable++;
    else legacy++;
  }
  return { count: mutable + legacy, mutable, legacy };
}

// The one-confirm dialog copy.
export function confirmText(summary) {
  const n = summary.count;
  let text = n === 1
    ? 'This strips 1 character to a blank. Its parts go to your Closet; the NFT stays in your wallet.'
    : `This strips ${n} characters to blanks. Their parts go to your Closet; the NFTs stay in your wallet.`;
  if (summary.legacy > 0) {
    const each = summary.legacy === 1 ? 'needs one Xaman accept' : 'need one Xaman accept each';
    text += ` ${summary.legacy} of them ${summary.legacy === 1 ? 'predates' : 'predate'} Dynamic NFTs and ${each} (burn + re-mint as a blank).`;
  } else {
    text += ' Nothing to sign — no Xaman taps needed.';
  }
  return text;
}

// Drop ids that are already being harvested. Used when a superseded batch
// response lands while a NEW picker selection is open: the older batch's
// started units must fall out of the new selection.
export function pruneSelection(selectedIds, harvestingIds) {
  const inflight = Array.isArray(harvestingIds) ? harvestingIds : [...(harvestingIds || [])];
  return (selectedIds || []).filter((id) => !inflight.includes(id));
}

// Partition per-unit server results into pollable starts vs rejections.
// A unit only counts as started when it carries a session_id.
export function splitBatchResults(results) {
  const started = [];
  const rejected = [];
  for (const r of results || []) {
    if (r.session_id) started.push(r);
    else rejected.push({ nft_id: r.nft_id, error: r.error || 'could not start the harvest' });
  }
  return { started, rejected };
}
