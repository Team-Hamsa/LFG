/**
 * share-card.mjs
 * ---------------------------------------------------------------------------
 * Renders the "Share on X" card for a single GO as a 1200x630 PNG
 * (Twitter/X `summary_large_image` size).
 *
 * The card is composed as an HTML/CSS document and rasterised with a headless
 * Chromium via Playwright. Everything the card needs — brand fonts and the LFG
 * logo — is resolved locally, so a render never depends on the network unless
 * you pass a remote URL for the avatar art.
 *
 * Data flow:
 *   renderShareCard({ tokenId, avatarSrc, logoSrc })
 *     -> buildHtml(...)                       // interpolate a fixed template
 *     -> page.setContent(html)                // load in headless Chromium
 *     -> page.evaluate(fit)                   // size #id + tagline to the column
 *     -> page.screenshot()                    // 1200x630 @2x PNG
 *
 * Fonts: install once ->  npm i @fontsource/fredoka @fontsource/jetbrains-mono
 * Browser: install once -> npx playwright install chromium   (skip if provided)
 * ---------------------------------------------------------------------------
 */

import { createRequire } from 'node:module';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const require = createRequire(import.meta.url);

// ---- Card geometry (keep in sync with the CSS in buildHtml) ----------------
const CARD_W = 1200;
const CARD_H = 630;
const SCALE = 2; // 2x for crisp text; output is 2400x1260 pixels

// Right-hand column width. Derived from the layout so the logo, the "#id"
// number, and the tagline can all share one width and one left edge:
//   stage padding (64 + 64) + avatar (460) + gap (54)  ->  column = 558
const COL_W = CARD_W - 64 - 64 - 460 - 54; // 558

// ---- Brand fonts -----------------------------------------------------------
// Resolved from the installed @fontsource packages so no font files need to be
// copied into your repo. Only the weights the card actually uses are loaded.
const FONT_FILES = {
  fredoka500: '@fontsource/fredoka/files/fredoka-latin-500-normal.woff2', // tagline
  fredoka700: '@fontsource/fredoka/files/fredoka-latin-700-normal.woff2', // #id
  mono500: '@fontsource/jetbrains-mono/files/jetbrains-mono-latin-500-normal.woff2', // domain
};

function fontFaceCss() {
  const url = (pkgPath) => pathToFileURL(require.resolve(pkgPath)).href;
  return `
    @font-face{font-family:'Fredoka';font-weight:500;src:url('${url(FONT_FILES.fredoka500)}') format('woff2');font-display:block}
    @font-face{font-family:'Fredoka';font-weight:700;src:url('${url(FONT_FILES.fredoka700)}') format('woff2');font-display:block}
    @font-face{font-family:'JetBrains Mono';font-weight:500;src:url('${url(FONT_FILES.mono500)}') format('woff2');font-display:block}`;
}

// ---- helpers ---------------------------------------------------------------

const MIME = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.webp': 'image/webp', '.gif': 'image/gif', '.svg': 'image/svg+xml',
};

/**
 * Resolve an image src for embedding in the card.
 * - http(s)/data URLs pass through untouched (loaded by the browser at render).
 * - local paths (or file: URLs) are read and inlined as base64 data URIs, so
 *   they render regardless of the document origin used by setContent.
 */
async function toSrc(pathOrUrl) {
  if (/^(https?:|data:)/i.test(pathOrUrl)) return pathOrUrl;
  const filePath = pathOrUrl.startsWith('file:') ? fileURLToPath(pathOrUrl) : pathOrUrl;
  const mime = MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
  const b64 = (await readFile(filePath)).toString('base64');
  return `data:${mime};base64,${b64}`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// ---- template --------------------------------------------------------------

/**
 * Build the card HTML. All copy/colour decisions live here; the only
 * per-render inputs are the token id, the avatar, the logo, the domain, and
 * the tagline markup.
 */
function buildHtml({ tokenId, avatarSrc, logoSrc, domain, taglineHtml, blue }) {
  const id = `#${escapeHtml(tokenId)}`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><style>
  :root{
    --yellow:#F0D848; --ink:#0A0A0A; --paper:#FFFFFF; --bg:#0A0A0A;
    --text:#F5F4F1; --muted:#9C9A94; --blue:${blue};
  }
  ${fontFaceCss()}
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{width:${CARD_W}px;height:${CARD_H}px}
  .stage{width:${CARD_W}px;height:${CARD_H}px;overflow:hidden;position:relative;
    background:radial-gradient(130% 90% at 72% -15%, #17120b 0%, var(--bg) 60%);
    font-family:'Fredoka',sans-serif;color:var(--text);
    display:flex;align-items:center;gap:54px;padding:0 64px}

  /* avatar — square art in a rounded "sticker" frame */
  .avatar{flex:0 0 auto;width:460px;height:460px;border-radius:30px;
    border:8px solid var(--paper);box-shadow:16px 16px 0 rgba(0,0,0,.55);
    overflow:hidden;background:#0A0A0A}
  .avatar img{width:100%;height:100%;display:block;object-fit:cover}

  /* fixed-width column so the lockup can never overflow */
  .col{flex:0 0 auto;width:${COL_W}px;height:460px;
    display:flex;flex-direction:column;align-items:flex-start;justify-content:center}
  .logo{width:100%;height:auto;display:block}
  .id{font-weight:700;font-size:150px;line-height:.9;color:var(--yellow);
    -webkit-text-stroke:5px var(--ink);paint-order:stroke fill;
    margin-top:22px;white-space:nowrap}
  .tag{font-weight:500;font-size:30px;color:var(--text);margin-top:24px;white-space:nowrap}
  .tag em{font-style:normal;color:var(--blue)}
  .domain{font-family:'JetBrains Mono',monospace;font-weight:500;font-size:18px;
    color:var(--muted);margin-top:22px}
</style></head>
<body>
  <div class="stage">
    <div class="avatar"><img src="${avatarSrc}" alt=""></div>
    <div class="col">
      <img class="logo" src="${logoSrc}" alt="LFG">
      <div class="id">${id}</div>
      <div class="tag">${taglineHtml}</div>
      <div class="domain">${escapeHtml(domain)}</div>
    </div>
  </div>
  <script>
    // Size the "#id" and the tagline to the column width so the logo, number,
    // and tagline share one width + left edge. The id only ever SHRINKS (long
    // ids like #10000 fit; short ids stay at the design size for consistency).
    window.__fit = function(){
      var colW = ${COL_W};
      var id = document.querySelector('.id');
      id.style.fontSize=''; var ib=parseFloat(getComputedStyle(id).fontSize);
      var iw=id.getBoundingClientRect().width;
      if(iw>colW) id.style.fontSize=(ib*colW/iw)+'px';
      var tag = document.querySelector('.tag');
      tag.style.fontSize=''; var tb=parseFloat(getComputedStyle(tag).fontSize);
      var tw=tag.getBoundingClientRect().width;
      if(tw>0) tag.style.fontSize=(tb*colW/tw)+'px';
    };
  </script>
</body></html>`;
}

// ---- public API ------------------------------------------------------------

/**
 * Render one share card to a PNG.
 *
 * @param {object}  opts
 * @param {string|number} opts.tokenId     GO number, e.g. 4035 (rendered as "#4035")
 * @param {string}  opts.avatarSrc         GO artwork — local path OR http(s)/data URL (square looks best)
 * @param {string}  opts.logoSrc           trimmed LFG logo PNG — local path OR URL
 * @param {string} [opts.domain]           footer domain text
 * @param {string} [opts.taglineHtml]      tagline markup; wrap the accent word in <em>
 * @param {string} [opts.blue]             accent colour for <em> (defaults to the logo's G blue)
 * @param {string} [opts.outPath]          if set, writes the PNG here; otherwise a Buffer is returned
 * @param {import('playwright').Browser} [opts.browser]  reuse a browser across many renders (batch)
 * @returns {Promise<Buffer>}              the PNG bytes (also written to outPath when provided)
 */
export async function renderShareCard(opts) {
  const {
    tokenId,
    avatarSrc,
    logoSrc,
    domain = 'build.letseffinggo.com',
    taglineHtml = 'What will <em>YOU</em> build?',
    blue = '#5A9FD0', // sampled from the logo's "G"
    outPath,
    browser,
  } = opts;

  if (tokenId == null) throw new Error('renderShareCard: tokenId is required');
  if (!avatarSrc) throw new Error('renderShareCard: avatarSrc is required');
  if (!logoSrc) throw new Error('renderShareCard: logoSrc is required');

  const html = buildHtml({
    tokenId,
    avatarSrc: await toSrc(avatarSrc),
    logoSrc: await toSrc(logoSrc),
    domain,
    taglineHtml,
    blue,
  });

  const ownBrowser = !browser;
  const b = browser || (await chromium.launch());
  try {
    const page = await b.newPage({
      viewport: { width: CARD_W, height: CARD_H },
      deviceScaleFactor: SCALE,
    });
    // `load` waits for the logo + avatar images; then wait for web fonts.
    await page.setContent(html, { waitUntil: 'load' });
    await page.evaluate(() => document.fonts.ready);
    await page.evaluate(() => window.__fit());
    const buf = await page.screenshot(outPath ? { path: outPath } : undefined);
    await page.close();
    return buf;
  } finally {
    if (ownBrowser) await b.close();
  }
}

export default renderShareCard;
