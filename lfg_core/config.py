# lfg_core/config.py
# Centralized environment configuration for the webapp/core modules.
# main.py keeps its own loading for backwards compatibility.

import os

from dotenv import load_dotenv

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

# Discord Activity (webapp only — not required by the bot)
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
WEBAPP_SESSION_SECRET = os.getenv("WEBAPP_SESSION_SECRET", "")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8176"))
ECONOMY_NETWORK = os.getenv("ECONOMY_NETWORK", "testnet")  # economy DB network
# Master switch for the Closet / dress-up trait economy surface. When off the
# service answers economy routes with 403 economy_disabled, registration does
# not auto-issue Closets, and the client hides the Dress Up UI — lets the
# Minter + Trait Swapper launch on mainnet before the Closet ships.
ECONOMY_ENABLED = os.getenv("ECONOMY_ENABLED", "1") not in ("0", "false", "False")
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
NFT_SCHEMA_URL = os.getenv(
    "NFT_SCHEMA_URL", "ipfs://QmNpi8rcXEkohca8iXu7zysKKSJYqCvBJn3xJwga8jXqWU"
)
EXTERNAL_WEBSITE_URL = os.getenv("EXTERNAL_WEBSITE_URL", "https://letseffinggo.com")
NFT_COLLECTION_LOGO = os.getenv(
    "NFT_COLLECTION_LOGO", "https://lfgo.b-cdn.net/LFGO_square_logo.png"
)

# Variable rarity engine
DB_PATH = os.getenv("DB_PATH", "lfg_nfts.db")
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
TRAIT_TAXON = int(os.getenv("TRAIT_TAXON", "1763"))
TRAIT_NFT_FLAGS = int(os.getenv("TRAIT_NFT_FLAGS", "9"))  # burnable(1)+transferable(8)
TRAIT_CDN_SUBDIR = os.getenv("TRAIT_CDN_SUBDIR", "traits")
