#!/usr/bin/env node
/**
 * render.mjs — CLI wrapper around share-card.mjs for the lfg_service
 * subprocess call:
 *   node render.mjs --token 4035 --avatar <path-or-url> --out out.png [--logo path]
 * Exit 0 = PNG written to --out. Exit 1 = failure (message on stderr).
 */
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { parseArgs } from 'node:util';
import renderShareCard from './share-card.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_LOGO = path.resolve(HERE, '../../assets/logo.png');

const { values } = parseArgs({
  options: {
    token: { type: 'string' },
    avatar: { type: 'string' },
    out: { type: 'string' },
    logo: { type: 'string', default: DEFAULT_LOGO },
  },
});

if (!values.token || !values.avatar || !values.out) {
  console.error('usage: render.mjs --token N --avatar <path|url> --out <path> [--logo <path>]');
  process.exit(1);
}

try {
  await renderShareCard({
    tokenId: values.token,
    avatarSrc: values.avatar,
    logoSrc: values.logo,
    outPath: values.out,
  });
} catch (err) {
  console.error(`render failed: ${err && err.message ? err.message : err}`);
  process.exit(1);
}
