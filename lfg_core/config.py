# lfg_core/config.py
# Centralized environment configuration for the webapp/core modules.
# main.py keeps its own loading for backwards compatibility.

import os

from lfg_core.db_path import app_db_path
from lfg_core.envload import load_dotenv_unless_skipped

# The pytest suite opts out of the deployed .env via LFG_SKIP_DOTENV=1 in the
# root conftest.py (#323) — see lfg_core/envload.py. Runtime (main.py / pm2)
# never sets the var, so it still loads normally.
load_dotenv_unless_skipped()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} not found in environment variables")
    return value


def env_flag(name: str, default: str = "0") -> bool:
    """Parse a boolean-ish env var (falsy denylist: "0"/"false"/"False").

    The shared idiom behind every feature flag below. It stays a callable so a
    test can exercise a flag's *shipped default* without depending on the
    ambient `.env`: the module constants are frozen at import time, and
    `load_dotenv()` walks up from CWD, so a developer box (or any git worktree
    under it) inherits whatever the deployed `.env` sets.
    """
    return os.getenv(name, default) not in ("0", "false", "False")


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
# The *_DEFAULT constants are named so tests can assert the shipped default
# without reading the frozen constant, which any ambient .env can override.
MAX_COLLECTION_SIZE_DEFAULT = 10000
BULK_MINT_MAX_DEFAULT = 10
MAX_COLLECTION_SIZE = int(os.getenv("MAX_COLLECTION_SIZE", str(MAX_COLLECTION_SIZE_DEFAULT)))
BULK_MINT_MAX = int(os.getenv("BULK_MINT_MAX", str(BULK_MINT_MAX_DEFAULT)))
if MAX_COLLECTION_SIZE < 1:
    raise ValueError(f"MAX_COLLECTION_SIZE must be >= 1, got {MAX_COLLECTION_SIZE}")
if BULK_MINT_MAX < 1:
    raise ValueError(f"BULK_MINT_MAX must be >= 1, got {BULK_MINT_MAX}")

# XRPL account reserves, in drops (#388/#408). Used by the destination
# pre-flight to refuse a mint whose recipient provably cannot hold the NFT.
# Constants rather than a per-mint `server_state` round-trip: these change only
# by amendment/validator vote, which is an ops edit, not a hot-path lookup.
# Live mainnet values as of 2026-08-23 (rippled 3.3.0): 1 XRP base, 0.2 XRP per
# owned object. Verify with:
#   curl -s -X POST https://s1.ripple.com:51234/ -H 'Content-Type: application/json' \
#     -d '{"method":"server_state"}' | jq '.result.state.validated_ledger
#       | {reserve_base, reserve_inc}'
XRPL_RESERVE_BASE_DROPS_DEFAULT = 1_000_000
XRPL_RESERVE_INC_DROPS_DEFAULT = 200_000
XRPL_RESERVE_BASE_DROPS = int(
    os.getenv("XRPL_RESERVE_BASE_DROPS", str(XRPL_RESERVE_BASE_DROPS_DEFAULT))
)
XRPL_RESERVE_INC_DROPS = int(
    os.getenv("XRPL_RESERVE_INC_DROPS", str(XRPL_RESERVE_INC_DROPS_DEFAULT))
)
if XRPL_RESERVE_BASE_DROPS < 0 or XRPL_RESERVE_INC_DROPS < 0:
    raise ValueError("XRPL_RESERVE_*_DROPS must be >= 0")

# Bulk mint UI flag (#215 follow-up): gates the Activity's quantity stepper /
# bulk flow client-side via /api/config. Server bulk endpoints stay live
# regardless (they're quantity-capped and auth'd on their own). Off by
# default; staging sets it first (docs/ops/env.staging.example).
BULK_MINT_UI_ENABLED_DEFAULT = "0"  # named so a test can lock the shipped default
BULK_MINT_UI_ENABLED = env_flag("BULK_MINT_UI_ENABLED", BULK_MINT_UI_ENABLED_DEFAULT)

# Burn-to-mint (#220): burn M of your own live LFG NFTs for M fresh mints —
# supply-neutral, so exempt from MAX_COLLECTION_SIZE. Ships dark: with the
# flag off the /api/mint/burn2mint endpoints refuse new sessions (403).
# Startup RESUME of already-burned sessions ignores the flag — a validated
# burn is irreversible and its owed mints must never depend on a config knob.
BURN_TO_MINT_ENABLED_DEFAULT = "0"  # named so a test can lock the shipped default
BURN_TO_MINT_ENABLED = env_flag("BURN_TO_MINT_ENABLED", BURN_TO_MINT_ENABLED_DEFAULT)

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
ECONOMY_ENABLED = env_flag("ECONOMY_ENABLED", "0")


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
# Pre-submit `simulate` pre-flight on backend-signed txs (#58). Read at CALL
# time via env_flag (never this constant) so the kill switch and the test pin
# both take effect without a restart/import-order dependency.
PRESUBMIT_SIMULATE_DEFAULT = "1"  # named so a test can lock the shipped default
MARKET_ENABLED_DEFAULT = "1"
MARKET_ENABLED = env_flag("MARKET_ENABLED", MARKET_ENABLED_DEFAULT)
WEBAPP_DEV_MODE = os.getenv("WEBAPP_DEV_MODE", "") not in ("", "0", "false", "False")

# Telegram Mini App (#89). All optional — the feature is OFF when unset:
# an empty bot token makes POST /api/telegram/auth return 503. The service and
# the Telegram bot read the same .env, so the bot token is available here.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# initData replay window (initData carries no nonce, so freshness is the guard).
TELEGRAM_INITDATA_MAX_AGE = int(os.getenv("TELEGRAM_INITDATA_MAX_AGE", "3600"))
if TELEGRAM_INITDATA_MAX_AGE <= 0:
    raise ValueError("TELEGRAM_INITDATA_MAX_AGE must be greater than 0")


def _parse_allowed_origins(raw: str) -> tuple[str, ...]:
    return tuple(o.strip() for o in raw.split(",") if o.strip())


# Standalone web surface (spec 2026-07-16): exact Origin values allowed to call
# the API cross-origin (the GitHub Pages front-end at build.letseffinggo.com).
# Empty (the default) keeps the CORS middleware inert — feature OFF.
WEB_ALLOWED_ORIGINS = _parse_allowed_origins(os.getenv("WEB_ALLOWED_ORIGINS", ""))

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
# The BRIX pair the trait economy is denominated in (shop prices, trait
# listings, the XRP→BRIX on-ramp). Defaults to the swap-fee pair above — the
# actual BRIX token per network — NOT TOKEN_* (the LFGO mint-payment token):
# on mainnet the two pairs differ, and an offer denominated in LFGO fails
# tecNO_LINE because the NFT issuer only holds a BRIX trustline for royalties.
BRIX_CURRENCY_HEX = os.getenv("BRIX_CURRENCY_HEX", SWAP_OFFER_CURRENCY_HEX)
BRIX_ISSUER = os.getenv("BRIX_ISSUER", SWAP_OFFER_ISSUER)
# Limit on the BRIX trustline the Activity sets for a holder (#441). Must
# exceed any single drip payout plus market activity — a line below an
# incoming claim makes that Payment fail tecPATH_DRY. Deliberately NOT
# TOKEN_TRUSTLINE_LIMIT (1000, sized for LFGO mint pricing).
BRIX_TRUSTLINE_LIMIT = os.getenv("BRIX_TRUSTLINE_LIMIT", "1000000000")
# Multiplier over the AMM spot quote when a swap fee is charged in XRP, so
# the follow-up BRIX buy-and-burn still clears if the pool moves slightly.
SWAP_XRP_FEE_BUFFER = os.getenv("SWAP_XRP_FEE_BUFFER", "1.05")
SWAP_MAX_NFT_NUMBER = int(os.getenv("SWAP_MAX_NFT_NUMBER", "3535"))
SWAP_RECORDS_DIR = os.getenv("SWAP_RECORDS_DIR", "swap_records")
# Distributor account for BRIX airdrops; used to classify history archive
# BRIX events as "airdrop" vs plain "payment".
BRIX_DISTRIBUTOR_ADDRESS = os.getenv("BRIX_DISTRIBUTOR_ADDRESS")
# Seed for the distributor wallet, used to SIGN daily-drip claim payouts (#48).
# Claims are paid from the distributor, never the BRIX issuer: paying from the
# issuer would silently mint new supply on every claim, while a pre-funded
# distributor keeps issuance an explicit, visible funding operation. Unset =
# the claim endpoints stay disabled (accrual still works and costs nothing).
BRIX_DISTRIBUTOR_SEED = os.getenv("BRIX_DISTRIBUTOR_SEED")
# Ledger margin for a claim Payment's LastLedgerSequence. This is what makes
# "definitively failed" decidable during recovery: past this ledger the XRPL
# guarantees the transaction can never validate, so an absent tx is proof of
# failure rather than a guess.
BRIX_CLAIM_LEDGER_MARGIN = int(os.getenv("BRIX_CLAIM_LEDGER_MARGIN", "40"))
# AMM account for LP token snapshots (testnet rLUnD5mskBnHfwFxCjakDA3RVgK584XQXG)
BRIX_AMM_ACCOUNT = os.getenv("BRIX_AMM_ACCOUNT")
NFT_SCHEMA_URL = os.getenv(
    "NFT_SCHEMA_URL", "ipfs://QmNpi8rcXEkohca8iXu7zysKKSJYqCvBJn3xJwga8jXqWU"
)
EXTERNAL_WEBSITE_URL = os.getenv("EXTERNAL_WEBSITE_URL", "https://letseffinggo.com")
NFT_COLLECTION_LOGO = os.getenv(
    "NFT_COLLECTION_LOGO", "https://lfgo.b-cdn.net/LFGO_square_logo.png"
)
# Silhouette art for a BLANK character (every slot "None"). Harvest points a
# stripped character's metadata `image` here; assemble replaces it with the
# composed art. Default is the pull-zone path scripts/upload_blank_art.py writes.
BLANK_IMAGE_URL = os.getenv(
    "BLANK_IMAGE_URL",
    f"https://{BUNNY_PULL_ZONE}/blank/silhouette.png" if BUNNY_PULL_ZONE else "",
)


DB_PATH = app_db_path(XRPL_NETWORK)

# Variable rarity engine
RARITY_FLOOR = float(os.getenv("RARITY_FLOOR", "0.005"))
# Share-ceiling cap (#198): clamp effective_weight's share term at
# RARITY_CAP_MULTIPLE × fair share (fair share = 1 / enabled-candidate-count
# in the pick). 0 or unset = no cap (behavior identical to the uncapped
# engine); 3.0 is the recommended opt-in value. The ceiling never sinks below
# floor_weight, and boosts multiply AFTER the cap.
RARITY_CAP_MULTIPLE_DEFAULT = 0.0  # named so a test can lock the shipped default
RARITY_CAP_MULTIPLE = float(os.getenv("RARITY_CAP_MULTIPLE", str(RARITY_CAP_MULTIPLE_DEFAULT)))
RARITY_BOOST_INITIAL = float(os.getenv("RARITY_BOOST_INITIAL", "7"))
RARITY_BOOST_STEP_HOURS = int(os.getenv("RARITY_BOOST_STEP_HOURS", "24"))

# Make Waves hackathon: every XRPL transaction / XUMM payload must carry this
# source tag or its volume does not count toward the hackathon.
SOURCE_TAG = int(os.getenv("SOURCE_TAG", "2606160021"))

# SourceTag-sponsored free mint campaign. Duration and cap are persisted on
# each activation; the explicit wallet list supplements the signing/issuer
# accounts that the store always excludes.
SPONSORED_MINT_DURATION_SECONDS = 3600
SPONSORED_MINT_CAP = 100
SPONSORED_MINT_ARCHIVE_MAX_LAG_SECONDS = int(
    os.getenv("SPONSORED_MINT_ARCHIVE_MAX_LAG_SECONDS", "900")
)
SPONSORED_MINT_ARCHIVE_GENESIS_HASHES = {
    "mainnet": os.getenv("SPONSORED_MINT_MAINNET_GENESIS_HASH", "").strip(),
    "testnet": os.getenv("SPONSORED_MINT_TESTNET_GENESIS_HASH", "").strip(),
}
# Listener self-heal (#402): on (re)subscribe, an index listener that finds a
# certified-but-gapped (bounded) eligibility archive kicks the bounded
# --catch-up-from-gap automatically in the background. On by default; the
# listener reads the flag live via env_flag at trigger time (never freeze it).
LISTENER_AUTO_CATCHUP_DEFAULT = "1"  # named so a test can lock the shipped default
LISTENER_AUTO_CATCHUP = env_flag("LISTENER_AUTO_CATCHUP", LISTENER_AUTO_CATCHUP_DEFAULT)
SPONSORED_MINT_EXCLUDED_WALLETS = tuple(
    value.strip()
    for value in os.getenv("SPONSORED_MINT_EXCLUDED_WALLETS", "").split(",")
    if value.strip()
)

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
TRAIT_TAXON_DEFAULT = 176  # flipped from 1763 (#217)
TRAIT_TAXON = int(os.getenv("TRAIT_TAXON", str(TRAIT_TAXON_DEFAULT)))
# Assemble-minted rebirth characters get their own taxon; regular /letsgo
# mints stay NFT_TAXON (0) so the main collection is never split (#217).
ASSEMBLE_TAXON_DEFAULT = 1760
ASSEMBLE_TAXON = int(os.getenv("ASSEMBLE_TAXON", str(ASSEMBLE_TAXON_DEFAULT)))

# Trait Shop (#217): whether the PROJECT itself sells freshly-minted traits.
# Default OFF — traits change hands between users (Extract -> list -> buy) and
# the shop is the only path where the project is the seller. Turning it off
# closes the catalog and the buy endpoint but leaves every user-to-user trait
# path, and the shop's own settlement/expiry sweep, running: an order already
# in flight when the flag flips still settles into the buyer's Closet.
SHOP_ENABLED_DEFAULT = "0"
SHOP_ENABLED = env_flag("SHOP_ENABLED", SHOP_ENABLED_DEFAULT)

# Trait Shop (#217): price = clamp(SHOP_BASE_BRIX / smoothed_share, MIN, MAX)
SHOP_BASE_BRIX = float(os.getenv("SHOP_BASE_BRIX", "1.0"))
SHOP_MIN_BRIX = int(os.getenv("SHOP_MIN_BRIX", "5"))
SHOP_MAX_BRIX = int(os.getenv("SHOP_MAX_BRIX", "5000"))
SHOP_OFFER_TTL_SECONDS = int(os.getenv("SHOP_OFFER_TTL_SECONDS", "900"))

# #283: on-ledger Expiration for native buy offers (bids) placed in-app.
# Bids escrow nothing, so they must always age out; default 7 days.
MARKET_BID_TTL_SECONDS = int(os.getenv("MARKET_BID_TTL_SECONDS", "604800"))

TRAIT_NFT_FLAGS = int(os.getenv("TRAIT_NFT_FLAGS", "9"))  # burnable(1)+transferable(8)
TRAIT_CDN_SUBDIR = os.getenv("TRAIT_CDN_SUBDIR", "traits")

# X (Twitter) integration (#41): a separate out-of-process poster surface
# (surfaces/x_bot/) tweets successful mints off the shared event firehose.
# All vars optional; SERVICE_TOKEN_X is intentionally NOT declared here — it
# follows the house pattern of living in the surface's own config module
# (surfaces/discord_bot/config.py, surfaces/telegram_bot/config.py precedent),
# not lfg_core/config.py.
X_CONSUMER_KEY = os.getenv("X_CONSUMER_KEY", "")
X_CONSUMER_SECRET = os.getenv("X_CONSUMER_SECRET", "")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
X_ACCESS_SECRET = os.getenv("X_ACCESS_SECRET", "")
# Self-imposed cap below the X API's pay-per-post tier cap: the Free tier no
# longer exists (~2026-02), pricing is $0.015/post + $0.20/post-with-a-URL.
# Posts here are deliberately link-free (2026-07-17 directive — the URL
# surcharge is exactly why), so at $0.015/post the default 100/month bounds
# worst-case spend at roughly $1.50/mo. A cost knob, not a rate-limit knob.
X_MONTHLY_POST_BUDGET = int(os.getenv("X_MONTHLY_POST_BUDGET", "100"))
# Master switch, true only when the flag is set AND all four OAuth 1.0a
# credentials are non-empty (mirrors the ECONOMY_ENABLED/MARKET_ENABLED
# boolean-flag idiom above: falsy denylist "0"/"false"/"False").
X_ENABLED = env_flag("X_ENABLED", "0") and all(
    (X_CONSUMER_KEY, X_CONSUMER_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET)
)
# sqlite state file for the poster process (dedup/budget/pause bookkeeping);
# read by both the poster (surfaces/x_bot/) and the service admin endpoints.
X_STATE_DB_PATH = os.getenv("X_STATE_DB_PATH", "x_state.db")

# Per-user X OAuth2 PKCE — "Share from my account" (#252, spec §7). Separate
# credential set from the brand poster's OAuth 1.0a block above: this is an
# OAuth 2.0 app client (confidential secret optional — public PKCE clients
# omit it). X_TOKEN_ENC_KEY is a Fernet key (generate with
# `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
# encrypting users' access/refresh tokens at rest in the identity DB.
# X_OAUTH_CALLBACK_URL must be the app's PUBLIC HTTPS callback
# (<public-base>/api/x/callback) registered on the X developer portal — the
# same public-HTTPS ops dependency as PUBLIC_SHARE_BASE_URL / Mini-App #89
# Part B. Feature-off posture: unless all three of client id, callback URL,
# and enc key are set, X_USER_SHARE_ENABLED is False and every /api/x/*
# route 404s (ships DARK; going live is an ops step).
X_OAUTH_CLIENT_ID = os.getenv("X_OAUTH_CLIENT_ID", "")
X_OAUTH_CLIENT_SECRET = os.getenv("X_OAUTH_CLIENT_SECRET", "")
X_OAUTH_CALLBACK_URL = os.getenv("X_OAUTH_CALLBACK_URL", "").strip()
X_TOKEN_ENC_KEY = os.getenv("X_TOKEN_ENC_KEY", "")
X_USER_SHARE_ENABLED = all((X_OAUTH_CLIENT_ID, X_OAUTH_CALLBACK_URL, X_TOKEN_ENC_KEY))
# Spend guards for POST /api/x/share (every accepted post bills the app
# account on X's pay-per-use API): per-wallet cooldown between posts, and a
# dedup window in which re-sharing the same (kind, nft_number) returns the
# cached tweet instead of a second paid post.
X_USER_SHARE_COOLDOWN_SECONDS = float(os.getenv("X_USER_SHARE_COOLDOWN_SECONDS", "60"))
X_USER_SHARE_DEDUP_SECONDS = float(os.getenv("X_USER_SHARE_DEDUP_SECONDS", "86400"))

# Public base URL the OG card page (GET /nft/{number}, lfg_service/app.py) uses
# to build its OWN absolute self-links (og:url, canonical) — NEVER derived from
# the request Host header, which is unstable across ingress paths (Discord's
# *.discordsays.com proxy vs the direct Tailscale Funnel .ts.net/lfg path).
# Unset (default) means the feature is off: the page renders normally but
# omits og:url/canonical rather than guessing a wrong/unstable URL (#41 §6.2).
PUBLIC_SHARE_BASE_URL = os.getenv("PUBLIC_SHARE_BASE_URL", "").strip().rstrip("/")

# Where a HUMAN clicking a share link is forwarded (JS location.replace on
# the OG card page, GET /nft/{number}) — e.g. https://build.letseffinggo.com.
# Never an HTTP redirect: X's crawler follows those and would render the
# destination's generic card instead of the per-NFT image. Unset (default)
# = feature off, the card page body renders exactly as before.
SHARE_FORWARD_URL = os.getenv("SHARE_FORWARD_URL", "").strip().rstrip("/")

# Branded share-card PNG rendering (GET /nft/{number}/card.png). Requires
# node + playwright chromium on the box (scripts/share_card/ — see the spec
# addendum). Off (default) = twitter:image keeps pointing at the raw art.
SHARE_CARD_RENDER_ENABLED = os.getenv("SHARE_CARD_RENDER_ENABLED", "0").strip() == "1"
