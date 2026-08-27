// Rebuild webapp/client/vendor/walletconnect.js.
//   cd scripts/vendor_walletconnect && npm ci && npm run build
// Same posture as vendor/embedded-app-sdk.js: the Activity is no-build vanilla JS
// and every third-party module is served same-origin, never from a CDN at runtime.
// esm.sh's `?bundle` output was NOT used here — it still imports /node/*.mjs
// polyfills and sub-chunks from esm.sh, so it is not actually self-contained.
import { build } from 'esbuild';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, '..', '..', 'webapp', 'client', 'vendor', 'walletconnect.js');
const pkg = JSON.parse(readFileSync(join(here, 'package.json'), 'utf8'));
const deps = Object.entries(pkg.dependencies).map(([k, v]) => `${k}@${v}`).join(', ');

await build({
  entryPoints: [join(here, 'entry.js')],
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: 'es2020',
  minify: true,
  define: { 'process.env.NODE_ENV': '"production"', global: 'globalThis' },
  banner: { js: `/* LFG vendored bundle — ${deps}; built by scripts/vendor_walletconnect/build.mjs (esbuild ${pkg.devDependencies.esbuild}). Do not edit; rebuild. */` },
  outfile: out,
  logLevel: 'info',
});
