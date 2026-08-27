# vendor_walletconnect

Produces `webapp/client/vendor/walletconnect.js` — the WalletConnect v2
`SignClient` + `WalletConnectModal`, plus a `Buffer` shim, as ONE self-contained
ESM file. It is what the Joey Wallet sign-in path (#399 Part 2, #447) will
`import()` at runtime, exactly like `vendor/embedded-app-sdk.js`.

```bash
cd scripts/vendor_walletconnect
npm ci
npm run build          # writes ../../webapp/client/vendor/walletconnect.js
```

Bump a version in `package.json`, rebuild, commit both. `node_modules/` is
gitignored; the lockfile is committed so the bundle is reproducible.

Usage sketch (client):

```js
const { SignClient, WalletConnectModal } = await import('./vendor/walletconnect.js?v=1');
const client = await SignClient.init({ projectId: window.LFG_CONFIG.reownProjectId, metadata: {...} });
// namespaces: { xrpl: { chains: ['xrpl:0'], methods: ['xrpl_signTransaction'], events: [] } }
```

The Reown project id is public client config (it ships in JS regardless); the
dashboard's origin allowlist is what gates abuse.

## Runtime network deps (ops)

The bundle itself is same-origin, but at runtime it dials
`wss://relay.walletconnect.com` (pairing/session relay) and
`https://api.web3modal.org` (modal wallet listings/icons). The web surface and
Telegram Mini App need nothing — the server sets no CSP. The **Discord
Activity** proxies all traffic, so those two hosts must be added as URL
Mappings in the dev portal (see `docs/ACTIVITY_SETUP.md` §1d) before the Joey
path can work inside Discord.
