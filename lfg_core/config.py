# lfg_core/config.py
# Centralized environment configuration for the webapp/core modules.
# main.py keeps its own loading for backwards compatibility.

import os

from dotenv import load_dotenv

from lfg_core.db_path import app_db_path

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} not found in environment variables")
    return value


# XUMM
XUMM_API_KEY = _require("XUMM_API_KEY")
XUMM_API_SECRET = _require("XUMM_API_SECRET")
XUMM_API_URL = os.getenv("XUMM_API_URL", "https://xumm.app/api/v1/platform/payload")

# XRPL
SEED = _require("SEED")
TOKEN_ISSUER_ADDRESS = _require("TOKEN_ISSUER_ADDRESS")
TOKEN_CURRENCY_HEX = _require("TOKEN_CURRENCY_HEX")
TOKEN_TRUSTLINE_LIMIT = os.getenv("TOKEN_TRUSTLINE_LIMIT", "1000")

# One flag flips network endpoints and the collection/BRIX issuer defaults
# between testnet (the SEED minter account issues everything) and mainnet
# (the original LFGO/BRIX issuer accounts). Individual env vars still win.
XRPL_NETWORK = os.getenv("XRPL_NETWORK", "mainnet").strip().lower()
IS_TESTNET = XRPL_NETWORK == "testnet"


def _seed_address() -> str:
    from xrpl.wallet import Wallet  # deferred: keep config import light

    try:
        return Wallet.from_seed(SEED).classic_address
    except Exception as e:
        raise ValueError(
            f"SEED is not a valid XRPL family seed (expected an 's…' base58 secret): {e}"
        ) from e


if IS_TESTNET:
    _default_rpc = "https://s.altnet.rippletest.net:51234/"
    _default_ws = "wss://s.altnet.rippletest.net:51233"
    _default_clio = "wss://clio.altnet.rippletest.net:51233"
    _default_swap_issuer = _seed_address()
    _default_brix_issuer = _default_swap_issuer
else:
    _default_rpc = "https://s1.ripple.com:51234/"
    _default_ws = "wss://xrplcluster.com"
    _default_clio = "wss://s2-clio.ripple.com"
    _default_swap_issuer = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
    _default_brix_issuer = "rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px"

# Account all bot-signed txs are submitted for. Defaults to the SEED-derived
# address (testnet: the seed IS the issuer). On mainnet the issuer signs via a
# regular key: SEED holds the regkey seed and SIGNING_ACCOUNT must be set to
# the issuer address (rLfgoMint…) — Wallet.from_seed would otherwise derive
# the regkey pair's own address and every issuer op would sign for the wrong
# account. Validated eagerly (like the SEED path) so a typo fails fast at
# startup instead of as an opaque temMALFORMED/actNotFound on every tx.
_signing_override = (os.getenv("SIGNING_ACCOUNT") or "").strip()
if _signing_override:
    from xrpl.core.addresscodec import is_valid_classic_address as _is_valid_addr

    if not _is_valid_addr(_signing_override):
        raise ValueError(
            f"SIGNING_ACCOUNT is not a valid XRPL classic address: {_signing_override!r}"
        )
SIGNING_ACCOUNT = _signing_override or _seed_address()

JSON_RPC_URL = os.getenv("XRPL_JSON_RPC_URL", _default_rpc)
WS_URL = os.getenv("XRPL_WS_URL", _default_ws)
# clio (XLS-46) endpoint. nft_info / nft_exists are clio-only methods — the
# plain rippled WS (WS_URL) answers them with `unknownCmd` -> None, which the
# fail-closed Closet on-ledger verify gate reads as "not owned" and refuses the
# op. Default to a clio host so those lookups work without per-deploy env tuning.
CLIO_WS_URL = os.getenv("XRPL_CLIO_WS_URL", _default_clio)

# NFT settings
NFT_TAXON = int(os.getenv("NFT_TAXON", "0"))
NFT_TRANSFER_FEE = int(os.getenv("NFT_TRANSFER_FEE", "7000"))
# XLS-20 / Dynamic NFTs NFToken flag bits.
NFT_FLAG_BURNABLE = 0x0001  # lsfBurnable — issuer may burn (required for Harvest)
NFT_FLAG_TRANSFERABLE = 0x0008  # tfTransferable
NFT_FLAG_MUTABLE = 0x0010  # tfMutable — Dynamic NFT, in-place NFTokenModify

# 25 = burnable + transferable + mutable. Burnable so the trait economy can
# harvest (issuer-burn) characters; mutable so trait swaps update in place
# (mutability, not burnability, selects the swap path — see swap_flow.py).
NFT_FLAGS = int(
    os.getenv(
        "NFT_FLAGS",
        str(NFT_FLAG_BURNABLE | NFT_FLAG_TRANSFERABLE | NFT_FLAG_MUTABLE),
    )
)
NFT_COLLECTION_NAME = os.getenv("NFT_COLLECTION_NAME", "Let's Effing Go!")

# Mint pricing. Holders with an LFGO trustline + balance pay MINT_PRICE_LFGO
# (sent to the issuer, i.e. burned). Wallets without one pay MINT_PRICE_XRP
# and the backend buys MINT_PRICE_LFGO off the DEX and burns it. The path is
# detected silently per-wallet; the user only ever sees their own price.
MINT_PRICE_LFGO = os.getenv("MINT_PRICE_LFGO", "1")
MINT_PRICE_XRP = os.getenv("MINT_PRICE_XRP", "10")

# Bulk minting (#215). MAX_COLLECTION_SIZE caps total live editions; a bulk
# request is clamped to the remaining headroom before payment. BULK_MINT_MAX
# caps how many a single bulk job may request.
MAX_COLLECTION_SIZE = int(os.getenv("MAX_COLLECTION_SIZE", "10000"))
BULK_MINT_MAX = int(os.getenv("BULK_MINT_MAX", "10"))
if MAX_COLLECTION_SIZE < 1:
    raise ValueError(f"MAX_COLLECTION_SIZE must be >= 1, got {MAX_COLLECTION_SIZE}")
if BULK_MINT_MAX < 1:
    raise ValueError(f"BULK_MINT_MAX must be >= 1, got {BULK_MINT_MAX}")

# BunnyCDN
BUNNY_CDN_ACCESS_KEY = _require("BUNNY_CDN_ACCESS_KEY")
BUNNY_CDN_STORAGE_ZONE = _require("BUNNY_CDN_STORAGE_ZONE")
BUNNY_CDN_BASE_URL = os.getenv("BUNNY_CDN_BASE_URL", "https://storage.bunnycdn.com").rstrip("/")
BUNNY_CDN_FOLDER = os.getenv("BUNNY_CDN_FOLDER", "minttest")
BUNNY_CDN_PUBLIC_BASE = os.getenv("BUNNY_CDN_PUBLIC_BASE", "https://lfgo.b-cdn.net")
# Custom domain for the same pull zone (bare hostname); legacy NFT metadata
# bakes this host into its image URLs, so the image proxy must allow both.
BUNNY_PULL_ZONE = os.getenv("BUNNY_PULL_ZONE", "").strip().rstrip("/")
IMG_PROXY_ALLOWED_BASES = tuple(
    {BUNNY_CDN_PUBLIC_BASE} | ({f"https://{BUNNY_PULL_ZONE}"} if BUNNY_PULL_ZONE else set())
)
# Host *suffixes* (https-only, matched against the parsed hostname) the image
# proxy also accepts: legacy mainnet NFTs carry ipfs:// image URIs, which
# swap_meta.resolve_ipfs turns into per-CID subdomains of this gateway (#153).
# The leading dot means a subdomain label is required — the bare gateway host
# or a look-alike containing the suffix mid-hostname cannot match.
IMG_PROXY_ALLOWED_HOST_SUFFIXES = (".ipfs.dweb.link",)

# Discord Activity (webapp only — not required by the bot)
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
WEBAPP_SESSION_SECRET = os.getenv("WEBAPP_SESSION_SECRET", "")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8176"))
# Economy DB network. Normalized like XRPL_NETWORK so the boot-time
# network-match assertion below compares apples to apples.
ECONOMY_NETWORK = os.getenv("ECONOMY_NETWORK", "testnet").strip().lower()
# Master switch for the Closet / dress-up trait economy surface. When off the
# service answers economy routes with 403 economy_disabled, registration does
# not auto-issue Closets, and the client hides the Dress Up UI — lets the
# Minter + Trait Swapper launch on mainnet before the Closet ships.
# Defaults OFF (opt-in): the economy signs on-ledger ops against XRPL_NETWORK
# while its DB/gates resolve on ECONOMY_NETWORK, so it must never be enabled
# unless an operator has deliberately confirmed both point at the same chain
# (see the assertion below and go-live review B5).
ECONOMY_ENABLED = os.getenv("ECONOMY_ENABLED", "0") not in ("0", "false", "False")


def validate_economy_config(
    economy_enabled: bool,
    economy_network: str,
    xrpl_network: str,
) -> None:
    """Refuse to boot the economy against a split network.

    The trait economy's DB reads/gates resolve on ECONOMY_NETWORK while its
    on-ledger ops (mint / burn / NFTokenModify) sign against XRPL_NETWORK's
    endpoints via the single-network xrpl_ops globals. If the two differ while
    the economy is live, reads run against one chain's DB while irreversible
    asset ops land on the other — the exact split-network hazard that
    ECONOMY_ENABLED=0 was pulled to prevent at the mainnet cutover (see
    reports/2026-07-11-trait-economy-golive-review.md, blocker B5). Enforce the
    invariant at startup instead of trusting an operator to keep two env vars
    in sync.

    Runs at import for every surface (all of them import config); raises
    ValueError so a misconfigured process fails fast and loudly rather than
    silently mutating assets on the wrong ledger.
    """
    if economy_enabled and economy_network != xrpl_network:
        raise ValueError(
            "ECONOMY_ENABLED is on but ECONOMY_NETWORK "
            f"({economy_network!r}) != XRPL_NETWORK ({xrpl_network!r}). "
            "The trait economy signs on-ledger ops against XRPL_NETWORK while "
            "its DB and gates resolve on ECONOMY_NETWORK; a split would land "
            "irreversible asset ops on the wrong chain. Set both to the same "
            "network, or ECONOMY_ENABLED=0."
        )


def assert_cli_network_match(network: str, xrpl_network: str = XRPL_NETWORK) -> None:
    """Fail fast when an economy CLI would read one chain's DB but sign another.

    The economy CLIs default `--network` to ECONOMY_NETWORK, but their on-ledger
    ops go through the single-network xrpl_ops globals bound to XRPL_NETWORK. The
    startup `validate_economy_config` assert only fires when ECONOMY_ENABLED — a
    manual CLI run (which is how ops drives harvest/assemble/equip/extract/deposit)
    bypasses it entirely, so an operator on a split deployment could read the
    testnet index while minting/burning on mainnet. Enforce the match at the DB
    open so no state-changing economy CLI can straddle two chains (bot review
    #187 / go-live review B5)."""
    if network != xrpl_network:
        raise ValueError(
            f"economy CLI --network {network!r} != XRPL_NETWORK ({xrpl_network!r}); "
            "the CLI reads the selected network's index/DB but signs on-ledger ops "
            "against XRPL_NETWORK, so a mismatch would land irreversible asset ops "
            "on the wrong chain. Run with matching env (ECONOMY_NETWORK == "
            "XRPL_NETWORK) or pass --network matching XRPL_NETWORK."
        )


validate_economy_config(ECONOMY_ENABLED, ECONOMY_NETWORK, XRPL_NETWORK)
# In-app marketplace (#44) feature flag (default on): when 0, every /api/market
# route answers 403 feature-disabled and the client hides the Marketplace UI —
# lets the Minter + Trait Swapper launch on mainnet before the money-touching
# marketplace (native NFTokenOffer list/buy/cancel) ships.
MARKET_ENABLED = os.getenv("MARKET_ENABLED", "1") not in ("0", "false", "False")
WEBAPP_DEV_MODE = os.getenv("WEBAPP_DEV_MODE", "") not in ("", "0", "false", "False")

# Telegram Mini App (#89). All optional — the feature is OFF when unset:
# an empty bot token makes POST /api/telegram/auth return 503. The service and
# the Telegram bot read the same .env, so the bot token is available here.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# initData replay window (initData carries no nonce, so freshness is the guard).
TELEGRAM_INITDATA_MAX_AGE = int(os.getenv("TELEGRAM_INITDATA_MAX_AGE", "3600"))
if TELEGRAM_INITDATA_MAX_AGE <= 0:
    raise ValueError("TELEGRAM_INITDATA_MAX_AGE must be greater than 0")

# Misc
PAYMENT_TIMEOUT_SECONDS = int(os.getenv("PAYMENT_TIMEOUT_SECONDS", "300"))
# After a credit-eligible payment wait times out, wait this long and re-check
# history once — a payment signed in time can validate seconds past the
# deadline and must not be silently kept (issue #196).
PAYMENT_GRACE_SECONDS = int(os.getenv("PAYMENT_GRACE_SECONDS", "15"))
# How long an unconsumed mint payment stays spendable as a credit. This is
# what bounds the credit backfill scan (a fixed floor would make the scan
# depth grow with issuer history forever, #197 review); older overpayments
# are refund territory, findable via the issue-196 reconciliation sweep.
MINT_CREDIT_TTL_SECONDS = int(os.getenv("MINT_CREDIT_TTL_SECONDS", str(30 * 86400)))

# Unified trait layer store (shared by mint + swap).
# Canonical structure: <body>/<TraitType>/<Value>.png|.gif|.mp4
LAYER_SOURCE = os.getenv("LAYER_SOURCE", "cdn")  # "cdn" or "local"
LAYERS_CDN_FOLDER = os.getenv("LAYERS_CDN_FOLDER", "layers")
LAYERS_DIR = os.getenv("LAYERS_DIR", "layers")  # local mode root
LAYER_CACHE_DIR = os.getenv("LAYER_CACHE_DIR", ".layer_cache")

# Trait Swapper (defaults follow XRPL_NETWORK; mainnet values match the
# original Trait-Swapper bot)
SWAP_ISSUER_ADDRESS = os.getenv("SWAP_ISSUER_ADDRESS", _default_swap_issuer)
SWAP_TAXON = int(os.getenv("SWAP_TAXON", "1760"))
SWAP_CDN_FOLDER = os.getenv("SWAP_CDN_FOLDER", "LFGO")
SWAP_OFFER_CURRENCY_HEX = os.getenv(
    "SWAP_OFFER_CURRENCY_HEX", "4252495800000000000000000000000000000000"
)  # BRIX
SWAP_OFFER_ISSUER = os.getenv("SWAP_OFFER_ISSUER", _default_brix_issuer)
SWAP_OFFER_AMOUNT = os.getenv("SWAP_OFFER_AMOUNT", "10")
# Multiplier over the AMM spot quote when a swap fee is charged in XRP, so
# the follow-up BRIX buy-and-burn still clears if the pool moves slightly.
SWAP_XRP_FEE_BUFFER = os.getenv("SWAP_XRP_FEE_BUFFER", "1.05")
SWAP_MAX_NFT_NUMBER = int(os.getenv("SWAP_MAX_NFT_NUMBER", "3535"))
SWAP_RECORDS_DIR = os.getenv("SWAP_RECORDS_DIR", "swap_records")
# Distributor account for BRIX airdrops; used to classify history archive
# BRIX events as "airdrop" vs plain "payment".
BRIX_DISTRIBUTOR_ADDRESS = os.getenv("BRIX_DISTRIBUTOR_ADDRESS")
# AMM account for LP token snapshots (testnet rLUnD5mskBnHfwFxCjakDA3RVgK584XQXG)
BRIX_AMM_ACCOUNT = os.getenv("BRIX_AMM_ACCOUNT")
NFT_SCHEMA_URL = os.getenv(
    "NFT_SCHEMA_URL", "ipfs://QmNpi8rcXEkohca8iXu7zysKKSJYqCvBJn3xJwga8jXqWU"
)
EXTERNAL_WEBSITE_URL = os.getenv("EXTERNAL_WEBSITE_URL", "https://letseffinggo.com")
NFT_COLLECTION_LOGO = os.getenv(
    "NFT_COLLECTION_LOGO", "https://lfgo.b-cdn.net/LFGO_square_logo.png"
)


DB_PATH = app_db_path(XRPL_NETWORK)

# Variable rarity engine
RARITY_FLOOR = float(os.getenv("RARITY_FLOOR", "0.005"))
RARITY_BOOST_INITIAL = float(os.getenv("RARITY_BOOST_INITIAL", "7"))
RARITY_BOOST_STEP_HOURS = int(os.getenv("RARITY_BOOST_STEP_HOURS", "24"))

# Make Waves hackathon: every XRPL transaction / XUMM payload must carry this
# source tag or its volume does not count toward the hackathon.
SOURCE_TAG = int(os.getenv("SOURCE_TAG", "2606160021"))

# Dress-up trait economy (Phase 2). Economy characters are minted burnable so
# the issuer can harvest (burn) them; the per-user Closet is a soulbound
# (non-transferable) mutable NFToken the issuer updates in place.
# Closet (per-user soulbound trait container; formerly "Bucket").
LEGACY_BUCKET_TAXON = int(os.getenv("BUCKET_TAXON", "1761"))
CLOSET_TAXON = int(os.getenv("CLOSET_TAXON", "1762"))
CLOSET_IMAGE_URL = os.getenv("CLOSET_IMAGE_URL", NFT_COLLECTION_LOGO)
ECONOMY_NFT_FLAGS = int(os.getenv("ECONOMY_NFT_FLAGS", "25"))  # burnable+transferable+mutable
CLOSET_NFT_FLAGS = int(os.getenv("CLOSET_NFT_FLAGS", "16"))  # mutable only (soulbound)
ECONOMY_RECORDS_DIR = os.getenv("ECONOMY_RECORDS_DIR", "economy_records")
ECONOMY_CDN_FOLDER = os.getenv("ECONOMY_CDN_FOLDER", SWAP_CDN_FOLDER)

# Standalone tradeable trait NFTokens (Phase 4). Burnable + transferable (NOT
# soulbound, NOT mutable); xrpl_ops.mint_nft applies NFT_TRANSFER_FEE to any
# transferable token, so the trait royalty is inherited (no separate constant).
TRAIT_TAXON = int(os.getenv("TRAIT_TAXON", "176"))  # flipped from 1763 (#217)
# Assemble-minted rebirth characters get their own taxon; regular /letsgo
# mints stay NFT_TAXON (0) so the main collection is never split (#217).
ASSEMBLE_TAXON = int(os.getenv("ASSEMBLE_TAXON", "1760"))

# Trait Shop (#217): price = clamp(SHOP_BASE_BRIX / smoothed_share, MIN, MAX)
SHOP_BASE_BRIX = float(os.getenv("SHOP_BASE_BRIX", "1.0"))
SHOP_MIN_BRIX = int(os.getenv("SHOP_MIN_BRIX", "5"))
SHOP_MAX_BRIX = int(os.getenv("SHOP_MAX_BRIX", "5000"))
SHOP_OFFER_TTL_SECONDS = int(os.getenv("SHOP_OFFER_TTL_SECONDS", "900"))

TRAIT_NFT_FLAGS = int(os.getenv("TRAIT_NFT_FLAGS", "9"))  # burnable(1)+transferable(8)
TRAIT_CDN_SUBDIR = os.getenv("TRAIT_CDN_SUBDIR", "traits")
