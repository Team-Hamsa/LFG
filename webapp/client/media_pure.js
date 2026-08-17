// webapp/client/media_pure.js
// #298: pure decision logic for the animated-art rendering strategy — which
// asset a dense GRID tile uses versus what a focused DETAIL view upgrades to.
// Grids are always the static image (PNG poster frame); only detail/focused
// views play the video, and only when the client can decode it. Node-tested
// in tests/test_media_pure_js.py (same harness as build_pure.js).
//
// Rows arrive in two field dialects: market/roster rows carry image/video
// (onchain_nfts columns, #377), economy characters carry image_url/video_url
// (economy_api). Both are accepted everywhere.

function imageOf(row) {
  if (!row) return null;
  return row.image || row.image_url || null;
}

function videoOf(row) {
  if (!row) return null;
  return row.video || row.video_url || null;
}

// Does this row's art play as video? Drives the grid tile "animated" badge.
export function isAnimated(row) {
  return Boolean(videoOf(row));
}

// Dense grid tile: ALWAYS the static image — never a decoder per tile.
// `animated` flags the tile so the UI can badge it (open the detail to play).
export function gridMedia(row) {
  return { image: imageOf(row), animated: isAnimated(row) };
}

// Focused/detail view: upgrade to the video when one exists AND the client
// can play it (canPlay=false — e.g. a webview without the codec — keeps the
// static image rather than a dead <video>).
export function detailMedia(row, canPlay = true) {
  return { image: imageOf(row), video: canPlay ? videoOf(row) : null };
}

// What a failed <video> degrades to. The inputs are the element's CURRENT
// poster/label — setMedia reuses one fixed-id element across renders, so the
// fallback must reflect the render in effect at error time, never
// creation-time closure values (a stale closure would resurrect the previous
// NFT's still). No poster → null: keep the video, never a broken <img>.
export function videoFallback(poster, label) {
  if (!poster) return null;
  return { src: poster, alt: label || '' };
}
