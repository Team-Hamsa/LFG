# Security Policy

## Non-custodial by design

LFG never holds user funds and never stores private keys. Every transaction —
mint, trait swap, marketplace list/buy, trustline — is signed by the user in
their own [Xaman (XUMM)](https://xaman.app/) wallet via QR or push. The backend
signs only issuer-side operations with its own wallet. There is no escrow and
no custodial holding: marketplace trades settle on native XRPL `NFTokenOffer`
objects directly between buyer and seller.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue.

- Preferred: open a [private security advisory](https://github.com/Team-Hamsa/LFG/security/advisories/new)
  on this repository.
- Include: affected surface (Discord bot, Telegram bot, Activity, service, or a
  `scripts/` tool), reproduction steps, and impact.

We aim to acknowledge reports within a few days. Once a fix is available and
deployed, we're happy to credit you in the advisory.

## Scope

In scope: the application code in `lfg_core/`, `lfg_service/`, `surfaces/`,
`webapp/`, and `scripts/`; the Xaman payload builders and XRPL transaction
paths; and authentication/identity handling in `lfg_service/`.

Out of scope: third-party services LFG integrates with (XRPL nodes, Xaman,
BunnyCDN, IPFS gateways, Discord/Telegram platforms) — report those to the
respective vendors.
