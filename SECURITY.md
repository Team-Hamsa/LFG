# Security Policy

## Trust model

**No user keys.** LFG never sees or stores a user's private key: every
user-side transaction (payments, marketplace list/buy/bid, trustlines, offer
accepts) is signed by the user in their own [Xaman](https://xaman.app/) wallet
via QR or push, or, on the web app, in Joey Wallet over WalletConnect.

**Project-side hot keys.** The backend does hold keys for the project's own
accounts, on the deploy box:

- `SEED` is the collection issuer's regular key. The issuer's master key is
  disabled, so this is the account's only signing authority. It signs mints,
  delivery offers, `NFTokenModify` updates, burns and buy-and-burns.
- `BRIX_DISTRIBUTOR_SEED` signs BRIX daily-drip claim payouts.
- `CLOSET_HOUSE_SEED` is used only for the house wallet's one-time setup.

**Where trust sits:**

- NFT marketplace trades are native XRPL `NFTokenOffer`s that settle directly
  between buyer and seller, with no custody.
- The **Closet Market** is backend-settled. A trait bid locks BRIX in an XRPL
  `TokenEscrow` payable to the app wallet, and the backend holds the
  (encrypted) fulfillment. A fill routes the buyer's funds through the app
  wallet before forwarding them to the seller.
- Closet contents are DB rows mirrored into issuer-mutable Closet NFT metadata.

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
