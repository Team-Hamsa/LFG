# Plan 3 — Discord Migration (Shared-Services Spine)

**Spec:** `docs/superpowers/specs/2026-06-25-discord-migration-design.md` (decisions D1–D5 LOCKED)
**Issue:** #53 · **Branch:** `feat/spine-plan3-discord` (already checked out)
**Depends on:** PR #76 (Plan 1 `lfg_service`) + PR #77 (Plan 2 Surface SDK `surfaces/_client`).

This plan inverts the Discord bot's **user-facing mint/register path** onto the
shared `lfg_service` via the Plan 2 `LFGServiceClient`, decomposes the
1,900-line `main.py` into `surfaces/discord_bot/`, and keeps `/admin` +
trustline bot-local (D1=B / D2=A). Each task ends with a green test run and a
commit, and each task is independently reviewable/mergeable.

---

## Global Constraints

- **LIVE bot, do NOT deploy.** The work is on `feat/spine-plan3-discord`. The
  implementer must **never** `pm2 restart`/`pm2 reload` `lfg-bot`. pm2 runs
  `python main.py`; the launch shim (Task 1) must keep `python main.py` working
  unchanged so deployment is a no-op git pull later.
- **Tests are repo-native SYNC style.** `def test_*` functions that drive
  coroutines through a local `_run(coro)`/`run(coro)` loop helper (see
  `tests/sdk_helpers.py::run`, `tests/test_xumm_source_tag.py::_run`). **NO
  pytest-asyncio.** Adapter tests use a **hand-written fake `LFGServiceClient`**
  (a plain object exposing only the methods under test) — they must **not** hit a
  real service, open a socket, or import aiohttp. discord.py `Interaction`,
  `Embed`, `followup`, etc. are mocked with simple stand-ins / `unittest.mock`.
- **mypy overrides.** Add `surfaces.discord_bot.*` to the **same relaxed
  override block** in `pyproject.toml` that already lists `main` (the
  `disallow_untyped_defs = false …` block at lines ~54–59) — it is high-churn
  Discord glue exactly like `main`/`webapp`; real-bug checks (arg-type,
  union-attr, return-value, attr-defined, call-arg) stay ON. **Do NOT** relax
  `surfaces._client.*` — the SDK stays fully strict.
- **SourceTag `2606160021` invariant.** Every XRPL tx / XUMM payload the bot
  triggers must carry `SourceTag = 2606160021`. Mint / offer / accept now flow
  through the service, which stamps it (`lfg_core.xrpl_ops`/`xumm_ops`,
  `config.SOURCE_TAG`). The **trustline `TrustSet`** stays bot-local and MUST be
  made to stamp it (Task 2) — this is itself part of the #75 fix.
- **Pre-commit gate is blocking** (ruff, ruff-format, mypy, gitleaks, pytest).
  Before every commit run:
  `.venv/bin/pre-commit run --files <changed files>`
  Use the repo venv: `/home/hamsa/LFG/.venv`. Run targeted tests with
  `.venv/bin/python -m pytest tests/<file>.py -q`.
- **No behavior change until Task 3.** Tasks 1–2 are pure relocation +
  scaffolding; the bot's runtime behavior is byte-for-byte identical until the
  `/register` inversion in Task 3 and the mint inversion in Task 4.
- **`trait_layers/` stays on disk (D5).** Do not delete the directory. Task 4
  deletes the *code* that composites from it, not the art.
- **Env additions:** `LFG_SERVICE_URL`, `SERVICE_TOKEN_DISCORD` (new). Document
  them in `CLAUDE.md`'s env block as part of Task 1.

---

## Task 1 — Scaffold `surfaces/discord_bot/`, `config.py`, launch shim, shared client lifecycle

Create the package, move config constants + `RetryBot`/bootstrap/`on_ready`/
`cleanup` into it, construct one shared `LFGServiceClient`, and reduce `main.py`
to a thin launch shim. **No behavior change** beyond the module split and the
client being constructed (it is not yet *called* by any handler).

**Files**
- NEW `surfaces/discord_bot/__init__.py`
- NEW `surfaces/discord_bot/config.py`
- NEW `surfaces/discord_bot/bot.py`
- NEW `tests/test_discord_config.py`
- NEW `tests/test_discord_launch_shim.py`
- EDIT `main.py` (becomes shim)
- EDIT `pyproject.toml` (mypy override)
- EDIT `CLAUDE.md` (env block — trivial, but commit with this task)

**Interfaces**
- `surfaces.discord_bot.config` exposes every constant `main.py` reads from env
  today (`DISCORD_BOT_TOKEN`, `ADMIN_LOG_CHANNEL_ID`, `SEED`,
  `TOKEN_ISSUER_ADDRESS`, `TOKEN_CURRENCY_HEX`, `TOKEN_TRUSTLINE_LIMIT`,
  `XUMM_API_KEY`/`SECRET`, `XUMM_API_URL`, `VIEW_TIMEOUT`, `RETRY_MAX_ATTEMPTS`,
  `RETRY_BASE_DELAY`, `EXTERNAL_WEBSITE_URL`, …) **plus** new
  `LFG_SERVICE_URL`, `SERVICE_TOKEN_DISCORD`. Keep the `_require(name)`
  fail-fast helper.
- `surfaces.discord_bot.bot.main()` — synchronous entry point that builds the
  bot and runs it (replaces the `if __name__ == "__main__"` block).

### Step 1.1 — package + config (write COMPLETE)

- [ ] Create `surfaces/discord_bot/__init__.py`:
  ```python
  # surfaces/discord_bot/ — the thin Discord adapter (was main.py). Slash-command
  # tree + views render results from the shared lfg_service via LFGServiceClient;
  # /admin + trustline stay bot-local (spec D1=B / D2=A).
  ```

- [ ] Create `surfaces/discord_bot/config.py` (COMPLETE — relocate the env block
  from `main.py:43–166`, verbatim values, plus the two new vars). It must NOT
  import discord/xrpl/etc. — pure env reading:
  ```python
  # surfaces/discord_bot/config.py
  # All environment-derived settings for the Discord adapter. Relocated from the
  # top of the legacy main.py (no value changes) plus the two new spine vars.
  import logging
  import os

  from dotenv import load_dotenv

  load_dotenv()


  def _require(name: str) -> str:
      value = os.getenv(name)
      if not value:
          raise ValueError(f"{name} not found in environment variables")
      return value


  # --- Discord ---
  DISCORD_BOT_TOKEN = _require("DISCORD_BOT_TOKEN")
  ADMIN_LOG_CHANNEL_ID = int(os.getenv("ADMIN_LOG_CHANNEL_ID", "0"))
  if not ADMIN_LOG_CHANNEL_ID:
      raise ValueError("ADMIN_LOG_CHANNEL_ID not found in environment variables")

  # --- Shared service (spine) ---
  LFG_SERVICE_URL = _require("LFG_SERVICE_URL")
  SERVICE_TOKEN_DISCORD = _require("SERVICE_TOKEN_DISCORD")

  # --- XUMM (trustline stays bot-local, D2=A) ---
  XUMM_API_KEY = _require("XUMM_API_KEY")
  XUMM_API_SECRET = _require("XUMM_API_SECRET")
  XUMM_API_URL = os.getenv("XUMM_API_URL", "https://xumm.app/api/v1/platform/payload")

  # --- XRPL / token (trustline payload) ---
  TOKEN_ISSUER_ADDRESS = _require("TOKEN_ISSUER_ADDRESS")
  TOKEN_CURRENCY_HEX = _require("TOKEN_CURRENCY_HEX")
  TOKEN_TRUSTLINE_LIMIT = os.getenv("TOKEN_TRUSTLINE_LIMIT", "1000")

  # --- UI / retry ---
  EXTERNAL_WEBSITE_URL = os.getenv("EXTERNAL_WEBSITE_URL", "https://letseffinggo.com")
  VIEW_TIMEOUT = int(os.getenv("VIEW_TIMEOUT", "600"))
  RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "5"))
  RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

  logging.basicConfig(
      level=logging.INFO,
      format="%(asctime)s - %(levelname)s - %(message)s",
      handlers=[logging.StreamHandler()],
  )
  ```
  > **Judgment call / cleanup:** `SEED`, `BUNNY_CDN_*`, `NFT_*`,
  > `METADATA_TEMPLATE`, the ffmpeg-presence check, and the XRPL/BunnyCDN client
  > singletons in `main.py:43–236` belong **only** to the parallel mint pipeline
  > deleted in Task 4. To keep Task 1 a pure relocation with no behavior change,
  > **carry `SEED` and the others into `config.py` for now** (Task 4 deletes the
  > now-unreferenced ones). The ffmpeg `shutil.which` guard moves to `bot.py`
  > and is **deleted in Task 4** (the bot no longer runs ffmpeg). Note this in
  > the Task 4 deletion list.

### Step 1.2 — bot.py bootstrap (write COMPLETE)

- [ ] Create `surfaces/discord_bot/bot.py` (COMPLETE). Relocate `RetryBot`
  (`main.py:190–211`), the `intents`/`bot`/`tree` setup (`184–214`), `cleanup`
  (`1318–1325`), `signal_handler` + registration (`1328–1338`), and `on_ready`
  (`1341–1346`). Add the shared `LFGServiceClient` construction. The slash
  commands and views are wired in via `tree` in later tasks; for Task 1 register
  nothing new — the bot just boots with the (empty) command tree so it is
  runnable end-to-end.
  ```python
  # surfaces/discord_bot/bot.py
  import asyncio
  import logging
  import random
  import signal

  import discord
  from discord.ext import commands

  from surfaces._client import LFGServiceClient
  from surfaces.discord_bot import config
  from user_db import create_users_table

  intents = discord.Intents.default()
  intents.message_content = True
  intents.members = True
  intents.presences = True


  class RetryBot(commands.Bot):
      async def start(self, *args, **kwargs):
          max_retries = config.RETRY_MAX_ATTEMPTS
          base_delay = config.RETRY_BASE_DELAY
          for attempt in range(max_retries):
              try:
                  if attempt > 0:
                      jitter = random.uniform(0, 2)
                      actual_delay = (base_delay * (2**attempt)) + jitter
                      logging.info(
                          f"Retry attempt {attempt + 1}/{max_retries} after {actual_delay:.2f}s delay"
                      )
                      await asyncio.sleep(actual_delay)
                  await super().start(*args, **kwargs)
                  return
              except Exception as e:
                  logging.error(f"Connection attempt {attempt + 1} failed: {e}")
                  if attempt == max_retries - 1:
                      raise


  bot = RetryBot(command_prefix="!", intents=intents)
  tree = bot.tree

  # One shared client for every handler (constructed here, entered in setup_hook).
  svc = LFGServiceClient(
      config.LFG_SERVICE_URL, config.SERVICE_TOKEN_DISCORD, "discord"
  )


  @bot.event
  async def setup_hook() -> None:
      # Enter the SDK's aiohttp session for the bot's lifetime.
      await svc.__aenter__()


  @bot.event
  async def on_ready() -> None:
      create_users_table()
      await tree.sync()
      assert bot.user is not None
      logging.info(f"Logged in as {bot.user} (ID: {bot.user.id})")


  async def cleanup() -> None:
      logging.info("Performing cleanup before shutdown...")
      try:
          await svc.close()
      except Exception as e:
          logging.error(f"Error closing service client: {e}")
      try:
          if not bot.is_closed():
              await bot.close()
      except Exception as e:
          logging.error(f"Error during cleanup: {e}")


  def _signal_handler(sig, frame) -> None:
      logging.info(f"Received signal {sig}, initiating shutdown...")
      loop = asyncio.get_event_loop()
      loop.create_task(cleanup())
      loop.stop()


  def main() -> None:
      signal.signal(signal.SIGINT, _signal_handler)
      signal.signal(signal.SIGTERM, _signal_handler)
      try:
          bot.run(config.DISCORD_BOT_TOKEN)
      except Exception as e:
          logging.error(f"Failed to start bot: {e}")


  if __name__ == "__main__":
      main()
  ```
  > **Wiring note:** later tasks import `bot`, `tree`, `svc`, `config` from this
  > module. Views/commands self-register on import, so `bot.py` will import
  > `views`, `admin`, and the command modules near the bottom (added in Tasks
  > 2–5). For Task 1 leave those imports out — nothing to register yet.

### Step 1.3 — launch shim (write COMPLETE)

- [ ] Replace the **entire** contents of `main.py` with the shim (this deletes
  all 1,900 lines; the kept code is relocated in Tasks 2–5, the deleted pipeline
  in Task 4). Keeping `python main.py` as the pm2 entrypoint is the whole point:
  ```python
  # main.py — launch shim. The Discord bot now lives in surfaces/discord_bot/.
  # pm2 runs `python main.py`; this keeps that entrypoint working unchanged.
  from surfaces.discord_bot.bot import main

  if __name__ == "__main__":
      main()
  ```
  > **Sequencing note for the implementer:** do the relocations (Tasks 2–5)
  > **before** flipping `main.py` to the shim if you want the old `main.py` as a
  > reference while moving code. Practically: in Task 1, instead of emptying
  > `main.py`, you may *temporarily* leave the legacy code in place and only add
  > the shim re-export at the very end of Task 5. **Recommended:** keep
  > `main.py` legacy code intact through Task 4's deletions, and convert it to
  > the pure shim in **Task 5's** final step. For Task 1, only create the
  > package + `config.py` + `bot.py`; have `bot.py` *also* be importable, and
  > assert the shim works via the test below by importing
  > `surfaces.discord_bot.bot:main` (not by emptying main.py yet). Mark the
  > "empty main.py to the shim" checkbox as a Task 5 deliverable.

  - [ ] **Decision (record in commit msg):** convert `main.py` to the shim **in
    Task 5**, after all kept code is relocated and the pipeline deleted. Task 1
    only proves `surfaces.discord_bot.bot:main` exists and imports cleanly.

### Step 1.4 — mypy + env docs

- [ ] In `pyproject.toml`, add `"surfaces.discord_bot.*"` to the `module = [...]`
  list of the relaxed-annotation override block (the one containing `"main",
  "lfg_service.app", "webapp.server", …`). Do not touch the `surfaces._client`
  strictness (it has no override → stays strict).
- [ ] In `CLAUDE.md`, add `LFG_SERVICE_URL` and `SERVICE_TOKEN_DISCORD` to the
  `.env` example block.

### Step 1.5 — tests (write COMPLETE)

- [ ] `tests/test_discord_config.py` — assert config loads with required env set,
  and that the new vars are present. Use `monkeypatch.setenv` + `importlib`:
  ```python
  import importlib

  REQUIRED = {
      "DISCORD_BOT_TOKEN": "tok",
      "ADMIN_LOG_CHANNEL_ID": "123",
      "LFG_SERVICE_URL": "http://svc",
      "SERVICE_TOKEN_DISCORD": "stk",
      "XUMM_API_KEY": "k",
      "XUMM_API_SECRET": "s",
      "TOKEN_ISSUER_ADDRESS": "rIssuer",
      "TOKEN_CURRENCY_HEX": "ABC",
  }


  def test_config_exposes_spine_vars(monkeypatch):
      for k, v in REQUIRED.items():
          monkeypatch.setenv(k, v)
      import surfaces.discord_bot.config as cfg

      cfg = importlib.reload(cfg)
      assert cfg.LFG_SERVICE_URL == "http://svc"
      assert cfg.SERVICE_TOKEN_DISCORD == "stk"
      assert cfg.VIEW_TIMEOUT == 600


  def test_config_fails_fast_without_service_url(monkeypatch):
      for k, v in REQUIRED.items():
          if k != "LFG_SERVICE_URL":
              monkeypatch.setenv(k, v)
      monkeypatch.delenv("LFG_SERVICE_URL", raising=False)
      import surfaces.discord_bot.config as cfg

      try:
          importlib.reload(cfg)
          raised = False
      except ValueError:
          raised = True
      assert raised
  ```
  > **Note:** `config.py` calls `load_dotenv()` at import. In CI a `.env` may
  > exist; `monkeypatch.setenv` overrides it, so the tests are deterministic. If
  > a stray `.env` injects extras, that is fine — the assertions only check the
  > vars under test.

- [ ] `tests/test_discord_launch_shim.py` — prove the package's `main` is
  importable as a callable without booting Discord (patch env first, never call
  `main()`):
  ```python
  def test_bot_main_is_importable(monkeypatch):
      for k, v in {
          "DISCORD_BOT_TOKEN": "tok",
          "ADMIN_LOG_CHANNEL_ID": "123",
          "LFG_SERVICE_URL": "http://svc",
          "SERVICE_TOKEN_DISCORD": "stk",
          "XUMM_API_KEY": "k",
          "XUMM_API_SECRET": "s",
          "TOKEN_ISSUER_ADDRESS": "rIssuer",
          "TOKEN_CURRENCY_HEX": "ABC",
      }.items():
          monkeypatch.setenv(k, v)
      from surfaces.discord_bot.bot import main

      assert callable(main)
  ```

- [ ] Run: `.venv/bin/python -m pytest tests/test_discord_config.py tests/test_discord_launch_shim.py -q`
- [ ] `.venv/bin/pre-commit run --files surfaces/discord_bot/__init__.py surfaces/discord_bot/config.py surfaces/discord_bot/bot.py tests/test_discord_config.py tests/test_discord_launch_shim.py pyproject.toml CLAUDE.md`
- [ ] **Commit:** `feat(discord): scaffold surfaces/discord_bot package + shared client lifecycle`

---

## Task 2 — Relocate admin (unchanged) + trustline (SourceTag-stamped) into the package

Move the **kept** code out of `main.py`. Admin is byte-for-byte unchanged
(still calls `lfg_core`/`db_helpers`/`rarity`/sqlite locally, D1=B). Trustline
moves to its own module AND gets `SourceTag = 2606160021` added to the
`TrustSet` payload (D2=A + #75 fix).

**Files**
- NEW `surfaces/discord_bot/admin.py`
- NEW `surfaces/discord_bot/trustline.py`
- NEW `tests/test_discord_trustline_sourcetag.py`
- EDIT `surfaces/discord_bot/bot.py` (import admin so `@tree.command` registers)

**Interfaces**
- `admin.py` exports `AdminView`, `BurnNFTModal`, `BurnConfirmView`,
  `NFTLookupModal`, `RarityOddsModal`, `RarityBoostModal`, `RarityDisableModal`,
  `burn_nft`, `log_admin_action`, and the `@tree.command(name="admin")`
  `admin_command`.
- `trustline.py` exports `create_trustline_request() -> dict | None` and
  `poll_trustline_status(interaction, trustline_data)` (the bounded poll loop),
  plus `safe_followup`.

### Step 2.1 — Relocate admin (MOVE, behavior unchanged)

- [ ] Create `surfaces/discord_bot/admin.py`. **MOVE** these `main.py` ranges
  verbatim (only fix imports):
  - `burn_nft` — `main.py:1349–1374`
  - `BurnNFTModal` — `1377–1449`
  - `BurnConfirmView` — `1452–1559`
  - `log_admin_action` — `1562–1569`
  - `RarityOddsModal` — `1572–1599`
  - `RarityBoostModal` — `1602–1658`
  - `RarityDisableModal` — `1661–1692`
  - `AdminView` — `1696–1779`
  - `admin_command` (the `@tree.command(name="admin")`) — `1782–1806`
  - `NFTLookupModal` — `1809–1892`
- [ ] **Import fixes at the top of `admin.py`:**
  ```python
  import asyncio
  import logging
  import sqlite3
  import traceback

  import discord
  from discord import Embed, TextStyle, app_commands
  from discord.ui import Button, Modal, TextInput, View
  from xrpl.clients import JsonRpcClient
  from xrpl.models.transactions import NFTokenBurn
  from xrpl.transaction import submit_and_wait
  from xrpl.wallet import Wallet

  from lfg_core import rarity as _rarity
  from surfaces.discord_bot import config
  from surfaces.discord_bot.bot import tree

  SEED = config.SEED
  JSON_RPC_URL = "https://s.altnet.rippletest.net:51234/"
  DATABASE = "lfg_nfts.db"
  ADMIN_LOG_CHANNEL_ID = config.ADMIN_LOG_CHANNEL_ID
  ```
  > `burn_nft` builds `NFTokenBurn` inline. The spec says "Burn still uses
  > `lfg_core` issuer-burn, which already stamps SourceTag." **Reality check
  > (judgment call):** the `main.py` `burn_nft` at 1349 builds a raw
  > `NFTokenBurn` with **no** SourceTag. To honor the invariant without a
  > behavior change to admin's flow, **add `source_tag=config_source_tag` to
  > this `NFTokenBurn`** (import `from lfg_core.config import SOURCE_TAG`). This
  > is the minimal correct stamping; it is in-scope as a #75 fix and is covered
  > by Task 6's SourceTag test. Keep the rest of admin untouched.
  - [ ] In `burn_nft`, change `NFTokenBurn(account=..., nftoken_id=nft_id)` →
    `NFTokenBurn(account=..., nftoken_id=nft_id, source_tag=SOURCE_TAG)` and add
    `from lfg_core.config import SOURCE_TAG`.

### Step 2.2 — Relocate trustline + ADD SourceTag (MOVE + fix)

- [ ] Create `surfaces/discord_bot/trustline.py`. **MOVE** `safe_followup`
  (`main.py:799–809`) and `create_trustline_request` (`751–796`); **extract**
  the bounded status-poll loop currently inline in `trustline_button`
  (`main.py:1183–1241`) into a reusable `async def poll_trustline_status(...)`.
- [ ] **ADD the SourceTag** to the `TrustSet` `transaction_json` in
  `create_trustline_request` (currently missing at `main.py:762–770`):
  ```python
  from lfg_core.config import SOURCE_TAG
  ...
  transaction_json = {
      "TransactionType": "TrustSet",
      "Flags": 131072,  # tfSetNoRipple
      "SourceTag": SOURCE_TAG,   # <-- ADDED (Make Waves invariant, #75)
      "LimitAmount": {
          "currency": config.TOKEN_CURRENCY_HEX,
          "issuer": config.TOKEN_ISSUER_ADDRESS,
          "value": config.TOKEN_TRUSTLINE_LIMIT,
      },
  }
  ```
  Keep the XUMM POST + the `requests`/`asyncio.to_thread` shape unchanged; just
  reference `config.*` and `config.XUMM_API_URL`, `config.XUMM_API_KEY/SECRET`.
- [ ] `poll_trustline_status` signature:
  `async def poll_trustline_status(interaction: discord.Interaction, trustline_data: dict) -> None`
  — body is the existing `if "uuid" in trustline_data:` deadline loop +
  terminal-state `safe_followup` calls, verbatim (lines 1188–1241), reading
  `config.XUMM_API_URL` etc.

### Step 2.3 — Wire admin into the bot

- [ ] At the bottom of `surfaces/discord_bot/bot.py`, add `import` of the admin
  module so its `@tree.command` and views register on bot import:
  ```python
  # Register handlers (import for side effects: @tree.command + View classes).
  from surfaces.discord_bot import admin  # noqa: E402,F401
  ```
  (Trustline has no slash command of its own — it is wired via `MintView` in
  Task 4; do not import it yet here to avoid an unused import.)

### Step 2.4 — Trustline SourceTag test (write COMPLETE)

- [ ] `tests/test_discord_trustline_sourcetag.py` — assert the built `TrustSet`
  payload carries `SourceTag = 2606160021`, mocking `requests.post`. Mirror
  `tests/test_xumm_source_tag.py` style:
  ```python
  import asyncio
  import os

  import pytest


  def _run(coro):
      loop = asyncio.new_event_loop()
      try:
          return loop.run_until_complete(coro)
      finally:
          loop.close()


  class _Resp:
      @staticmethod
      def json():
          return {"refs": {"qr_png": "q"}, "next": {"always": "n"}, "uuid": "u"}


  @pytest.fixture
  def trustline(monkeypatch):
      for k, v in {
          "DISCORD_BOT_TOKEN": "t",
          "ADMIN_LOG_CHANNEL_ID": "1",
          "LFG_SERVICE_URL": "http://svc",
          "SERVICE_TOKEN_DISCORD": "s",
          "XUMM_API_KEY": "k",
          "XUMM_API_SECRET": "s",
          "TOKEN_ISSUER_ADDRESS": "rIssuer",
          "TOKEN_CURRENCY_HEX": "ABC",
      }.items():
          monkeypatch.setenv(k, v)
      import importlib

      import surfaces.discord_bot.config as cfg

      importlib.reload(cfg)
      import surfaces.discord_bot.trustline as tl

      importlib.reload(tl)
      return tl


  def test_trustline_payload_has_source_tag(trustline, monkeypatch):
      captured = {}

      def fake_post(url, json, headers, timeout=None):
          captured["payload"] = json
          return _Resp()

      monkeypatch.setattr(trustline.requests, "post", fake_post)
      _run(trustline.create_trustline_request())
      assert captured["payload"]["txjson"]["SourceTag"] == 2606160021
  ```
  > **Import-order note:** `admin.py` and `trustline.py` import
  > `surfaces.discord_bot.bot` (for `tree`). `bot.py` builds the real
  > `LFGServiceClient` and `discord.Bot` at import — that is fine (no network
  > until `bot.run`), but the test must set env first (the fixture does). If the
  > `bot` import is heavy for the trustline test, keep `tree` import only in
  > `admin.py`; `trustline.py` itself needs no `tree` (it has no slash command),
  > so `trustline.py` should NOT import `bot` — import only `config` and
  > `lfg_core.config`. Adjust the relocation accordingly.

### Step 2.5 — gate

- [ ] Run: `.venv/bin/python -m pytest tests/test_discord_trustline_sourcetag.py -q`
- [ ] `.venv/bin/pre-commit run --files surfaces/discord_bot/admin.py surfaces/discord_bot/trustline.py surfaces/discord_bot/bot.py tests/test_discord_trustline_sourcetag.py`
- [ ] **Commit:** `refactor(discord): relocate admin + trustline into package; stamp SourceTag on TrustSet`

---

## Task 3 — `/register` → `client.register(...)`

Replace the direct `register_user(...)` call with the SDK call so the service
performs the `identities` + `Users` dual-write (Plan 1). This is the **first
behavior-affecting** task (the write now goes through the service).

**Files**
- NEW `surfaces/discord_bot/commands.py` (holds `/register` and `/letsgo`)
- NEW `tests/test_discord_register.py`
- EDIT `surfaces/discord_bot/bot.py` (import commands for registration)

**Interfaces**
- `commands.py` defines `@tree.command(name="register")` `register(interaction, wallet)`
  and `@tree.command(name="letsgo")` `mint(interaction)` (the `/letsgo` panel,
  relocated from `main.py:1261–1295`).

### Step 3.1 — commands.py (write COMPLETE for /register; MOVE /letsgo)

- [ ] Create `surfaces/discord_bot/commands.py`. **MOVE** `/letsgo` `mint`
  (`main.py:1261–1295`) verbatim (it constructs `MintView()`, imported from
  `views` — see Task 4; for Task 3, `MintView` does not exist yet, so import it
  lazily inside the handler or stub the import). **Recommended ordering:** since
  `/letsgo` needs `MintView` (Task 4), relocate `/letsgo` in Task 4 alongside
  `views.py`, and in **Task 3 put only `/register`** in `commands.py`. Keep this
  task minimal:
  ```python
  # surfaces/discord_bot/commands.py
  import discord

  from surfaces.discord_bot.bot import svc, tree
  from surfaces._client.errors import ServiceError


  @tree.command(name="register", description="Register your wallet")
  async def register(interaction: discord.Interaction, wallet: str) -> None:
      """Register the caller's wallet via the shared service (dual-writes
      identities + Users)."""
      discord_id = str(interaction.user.id)
      discord_name = str(interaction.user)
      try:
          await svc.register(discord_id, discord_name, wallet)
      except ServiceError as e:
          msg = e.message or "There was an error registering your wallet."
          await interaction.response.send_message(msg, ephemeral=True)
          return
      await interaction.response.send_message(
          "Your wallet has been registered!", ephemeral=True
      )
  ```

### Step 3.2 — register commands on the bot

- [ ] In `surfaces/discord_bot/bot.py`, add to the side-effect import block:
  ```python
  from surfaces.discord_bot import commands  # noqa: E402,F401
  ```

### Step 3.3 — adapter test with a fake client (write COMPLETE)

- [ ] `tests/test_discord_register.py` — drive the `register` callback with a
  **fake `svc`** and a fake interaction; assert it calls `svc.register` with the
  right args and sends the success message; assert `ServiceError` → failure
  message. Patch the module-level `svc`:
  ```python
  import asyncio
  from unittest.mock import AsyncMock, MagicMock

  import pytest

  from surfaces._client.errors import BadRequest


  def _run(coro):
      loop = asyncio.new_event_loop()
      try:
          return loop.run_until_complete(coro)
      finally:
          loop.close()


  @pytest.fixture
  def reg(monkeypatch):
      for k, v in {
          "DISCORD_BOT_TOKEN": "t",
          "ADMIN_LOG_CHANNEL_ID": "1",
          "LFG_SERVICE_URL": "http://svc",
          "SERVICE_TOKEN_DISCORD": "s",
          "XUMM_API_KEY": "k",
          "XUMM_API_SECRET": "s",
          "TOKEN_ISSUER_ADDRESS": "rI",
          "TOKEN_CURRENCY_HEX": "ABC",
      }.items():
          monkeypatch.setenv(k, v)
      import importlib

      import surfaces.discord_bot.config as cfg

      importlib.reload(cfg)
      import surfaces.discord_bot.commands as cmds

      importlib.reload(cmds)
      return cmds


  def _fake_interaction():
      ix = MagicMock()
      ix.user.id = 42
      ix.user.__str__ = lambda self: "alice#0001"
      ix.response.send_message = AsyncMock()
      return ix


  def _callback(cmd):
      # discord app_commands.Command stores the coroutine on .callback
      return cmd.callback


  def test_register_calls_service_and_confirms(reg, monkeypatch):
      fake_svc = MagicMock()
      fake_svc.register = AsyncMock(return_value={"ok": True})
      monkeypatch.setattr(reg, "svc", fake_svc)
      ix = _fake_interaction()
      _run(_callback(reg.register)(ix, "rWALLET"))
      fake_svc.register.assert_awaited_once_with("42", "alice#0001", "rWALLET")
      ix.response.send_message.assert_awaited_once()
      assert "registered" in ix.response.send_message.call_args.args[0].lower()


  def test_register_maps_service_error_to_failure(reg, monkeypatch):
      fake_svc = MagicMock()
      fake_svc.register = AsyncMock(side_effect=BadRequest("bad wallet", status=400))
      monkeypatch.setattr(reg, "svc", fake_svc)
      ix = _fake_interaction()
      _run(_callback(reg.register)(ix, "nope"))
      ix.response.send_message.assert_awaited_once()
  ```
  > **Note on `app_commands.Command`:** `@tree.command` wraps the function in a
  > `discord.app_commands.Command`; the original coroutine is at `.callback`.
  > If the real attribute name differs in the installed discord.py, the helper
  > `_callback` is the single place to fix it. If patching the wrapped object is
  > awkward, an alternative is to define the handler as a plain
  > `async def _register_impl(interaction, wallet, *, svc)` and have the
  > `@tree.command` shell call it — test `_register_impl` directly with the fake
  > svc. Use whichever keeps the test clean; prefer testing the impl function.

### Step 3.4 — gate

- [ ] Run: `.venv/bin/python -m pytest tests/test_discord_register.py -q`
- [ ] `.venv/bin/pre-commit run --files surfaces/discord_bot/commands.py surfaces/discord_bot/bot.py tests/test_discord_register.py`
- [ ] **Commit:** `feat(discord): /register routes through the shared service`

---

## Task 4 — Invert the Mint button onto the SDK; DELETE the parallel pipeline

Replace the entire inline mint pipeline in `mint_button` with SDK calls:
`start_mint` → render payment QR → `wait_for_mint` → render offer-accept QR,
mapping `ServiceError` codes to friendly embeds. Relocate `MintView` (mint +
trustline + buy buttons) and `/letsgo` into the package. **Delete** the dead
pipeline functions. This is the core of the migration.

**Files**
- NEW `surfaces/discord_bot/render.py` (embed/QR builders)
- NEW `surfaces/discord_bot/mint_view.py` (the inverted mint handler)
- NEW `surfaces/discord_bot/views.py` (`MintView` shell wiring mint/trustline/buy)
- EDIT `surfaces/discord_bot/commands.py` (add `/letsgo` using `MintView`)
- EDIT `surfaces/discord_bot/bot.py` (import views/commands)
- NEW `tests/test_discord_mint.py`
- EDIT `main.py` → convert to the pure launch shim (deletes the legacy file)
- EDIT `surfaces/discord_bot/config.py` (drop now-dead pipeline constants)

### Step 4.1 — Map the service contract (read, no code)

- [ ] Confirm the mint session shapes the SDK returns (from `lfg_service`
  `mint_flow` / `MintSession.to_dict()`):
  - `start_mint` returns a dict with `session_id`, `state` (initially
    `awaiting_payment`), and payment fields (`payment_link` and/or a QR `d=`
    string / `qr_png` URL — confirm exact keys in `lfg_service/mint_flow.py`
    and `app.py::handle_mint`).
  - `wait_for_mint` returns the terminal session dict; terminal states are
    `offer_ready` / `done` / `failed` / `payment_timeout` (`MINT_TERMINAL` in
    the SDK). On `offer_ready`/`done` it carries `nft_number`, `image_url`, and
    an **accept** link/QR string. Record the exact key names in `render.py`
    docstrings so the handler reads real fields.
  > **Judgment call:** the spec lists `client.qr_png(data)` for server-rendered
  > QR. Prefer reading the session's returned `payment_link` and calling
  > `await svc.qr_png(payment_link)` to get PNG bytes → attach as a
  > `discord.File`. If the session already exposes a hosted QR URL, set it via
  > `embed.set_image(url=...)` and skip `qr_png`. Pick based on the actual
  > session keys; the test mocks both so either renders.

### Step 4.2 — render.py (write COMPLETE)

- [ ] Create `surfaces/discord_bot/render.py` — pure builders, no SDK calls, so
  they are trivially unit-testable:
  ```python
  # surfaces/discord_bot/render.py
  # Embed/QR builders shared by the mint handler. Pure functions: given session
  # dicts (already fetched from the service) they return discord.Embed objects.
  from typing import Any

  import discord
  from discord import Embed

  from surfaces.discord_bot import config


  def payment_embed(payment_link: str) -> Embed:
      embed = Embed(
          title="💰 Token Payment Required",
          description=(
              "Please pay 1 token to mint your NFT.\n\n"
              "**Steps:**\n"
              "1. Scan the QR code with your XRPL wallet (XUMM, Xaman, etc.)\n"
              "2. Approve the payment\n"
              "3. Wait for confirmation\n\n"
              f"[Open Payment Link]({payment_link})"
          ),
          color=0x00FF00,
      )
      embed.set_footer(text="Payment request expires in 5 minutes")
      embed.set_image(url="attachment://payment_qr.png")
      return embed


  def offer_embed(final: dict[str, Any]) -> Embed:
      number = final.get("nft_number", "?")
      accept_url = final.get("accept_url") or final.get("xumm_url", "")
      embed = Embed(
          title="🎨 NFT Minted Successfully!",
          description=(
              "Your NFT has been minted and an offer has been created!\n\n"
              f"**NFT Number:** #{number}\n"
              "**To claim your NFT:**\n"
              "1. Scan the QR code with XUMM\n"
              "2. Review and accept the offer\n"
              "3. Your NFT will appear in your wallet!\n\n"
              f"[Open in XUMM]({accept_url})"
          ),
          color=0x00FF00,
      )
      image_url = final.get("image_url")
      if image_url:
          embed.set_thumbnail(url=image_url)
      embed.set_image(url="attachment://offer_qr.png")
      embed.set_footer(text="Offer acceptance request expires in 24 hours")
      return embed


  def error_embed(message: str) -> Embed:
      return Embed(title="⚠️ Mint failed", description=message, color=0xFF0000)


  def file_from_png(data: bytes, filename: str) -> discord.File:
      import io

      return discord.File(io.BytesIO(data), filename=filename)
  ```
  > Adjust `accept_url`/`nft_number`/`image_url` key names to the real session
  > dict confirmed in Step 4.1.

### Step 4.3 — mint_view.py (write COMPLETE — the inverted handler)

- [ ] Create `surfaces/discord_bot/mint_view.py` with the inverted handler as a
  standalone, testable coroutine `handle_mint(svc, interaction)` so the test
  drives it with a fake svc + fake interaction (no `View` plumbing needed):
  ```python
  # surfaces/discord_bot/mint_view.py
  # The inverted mint handler: start_mint -> payment QR -> wait_for_mint ->
  # offer-accept QR. All XRPL work happens in the service (which stamps
  # SourceTag); this module only orchestrates SDK calls + renders embeds.
  import logging

  import discord

  from surfaces._client import LFGServiceClient
  from surfaces._client.errors import BadRequest, ServiceError
  from surfaces.discord_bot import render

  MINT_OK_STATES = frozenset({"offer_ready", "done"})


  def _friendly(err: ServiceError) -> str:
      code = (err.code or "").lower()
      if isinstance(err, BadRequest) and ("wallet" in code or "wallet" in (err.message or "").lower()):
          return "Please register your wallet first using /register."
      if err.status == 409 or "in_progress" in code or "already" in (err.message or "").lower():
          return "You already have a mint in progress — finish or wait for it to time out."
      return err.message or "The mint service is unavailable. Please try again shortly."


  async def handle_mint(svc: LFGServiceClient, interaction: discord.Interaction) -> None:
      await interaction.response.defer(ephemeral=True)
      user_id = str(interaction.user.id)
      username = str(interaction.user)

      # 1. start
      try:
          session = await svc.start_mint(user_id, username=username)
      except ServiceError as e:
          await interaction.followup.send(
              embed=render.error_embed(_friendly(e)), ephemeral=True
          )
          return

      session_id = session["session_id"]
      payment_link = session.get("payment_link", "")

      # 2. payment QR
      try:
          qr_png = await svc.qr_png(payment_link)
          file = render.file_from_png(qr_png, "payment_qr.png")
          await interaction.followup.send(
              embed=render.payment_embed(payment_link), file=file, ephemeral=True
          )
      except ServiceError as e:
          logging.error(f"payment QR render failed: {e}")
          await interaction.followup.send(
              embed=render.error_embed(_friendly(e)), ephemeral=True
          )
          return

      # 3. wait for terminal state (SDK polls + backs off)
      try:
          final = await svc.wait_for_mint(user_id, session_id)
      except ServiceError as e:
          await interaction.followup.send(
              embed=render.error_embed(_friendly(e)), ephemeral=True
          )
          return

      state = final.get("state")
      if state not in MINT_OK_STATES:
          reason = {
              "payment_timeout": "Payment request timed out. Please try again.",
              "failed": "The mint failed. Please try again or contact an admin.",
          }.get(state, "Mint did not complete. Please try again.")
          await interaction.followup.send(
              embed=render.error_embed(reason), ephemeral=True
          )
          return

      # 4. offer-accept QR
      accept_link = final.get("accept_url") or final.get("xumm_url", "")
      try:
          qr_png = await svc.qr_png(accept_link)
          file = render.file_from_png(qr_png, "offer_qr.png")
      except ServiceError:
          file = None
      await interaction.followup.send(
          embed=render.offer_embed(final),
          **({"file": file} if file else {}),
          ephemeral=True,
      )
  ```
  > Confirm whether the offer QR uses a hosted `qr_url` (then `set_image(url=)`
  > and drop the `qr_png` call) vs. a `d=` payload (then `qr_png`). The handler
  > above renders bytes; if hosted, simplify and drop the `attachment://` image
  > in `offer_embed`. Lock this against the real session shape in Step 4.1.

### Step 4.4 — views.py (write COMPLETE — thin View shell)

- [ ] Create `surfaces/discord_bot/views.py`. `MintView` keeps the three buttons
  but delegates: mint → `handle_mint(svc, interaction)`; trustline →
  `trustline.create_trustline_request()` + `trustline.poll_trustline_status(...)`
  (relocated body from `main.py:1138–1258`, minus the deleted payment logic);
  buy → the URL button (unchanged):
  ```python
  # surfaces/discord_bot/views.py
  from typing import Any

  import discord
  from discord import Embed
  from discord.ui import Button, View

  from surfaces.discord_bot import config, trustline
  from surfaces.discord_bot.bot import svc
  from surfaces.discord_bot.mint_view import handle_mint
  from surfaces.discord_bot.trustline import safe_followup


  class MintView(View):
      def __init__(self) -> None:
          super().__init__(timeout=config.VIEW_TIMEOUT)
          self.buy_button: Button[Any] = Button(
              label="💰 Buy Token",
              style=discord.ButtonStyle.success,
              url=config.EXTERNAL_WEBSITE_URL,
          )
          self.add_item(self.buy_button)

      @discord.ui.button(label="🎨 Mint NFT", style=discord.ButtonStyle.primary)
      async def mint_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
          await handle_mint(svc, interaction)

      @discord.ui.button(label="🔗 Set LFGO Trustline", style=discord.ButtonStyle.secondary)
      async def trustline_button(
          self, interaction: discord.Interaction, button: Button[Any]
      ) -> None:
          await interaction.response.defer(ephemeral=True)
          data = await trustline.create_trustline_request()
          if not data:
              await safe_followup(
                  interaction,
                  "Failed to create trustline request. Please try again.",
                  ephemeral=True,
              )
              return
          embed = Embed(
              title="🔗 Set Up LFGO Token Trustline",
              description=(
                  "Please set up a trustline for the LFGO token.\n\n"
                  "**Steps:**\n"
                  "1. Scan the QR code with your XUMM app\n"
                  "2. Review and approve the trustline\n"
                  "3. Wait for confirmation\n\n"
                  f"[Open in XUMM]({data['xumm_url']})"
              ),
              color=0x00FF00,
          )
          embed.set_image(url=data["qr_url"])
          embed.set_footer(text="Trustline request expires in 5 minutes")
          await safe_followup(interaction, embed=embed, ephemeral=True)
          await trustline.poll_trustline_status(interaction, data)
  ```
  > The trustline button body is the relocated, unchanged behavior (the
  > `get_user` wallet-required check from `main.py:1143–1148` may be kept or
  > dropped — it was a UX guard, not correctness; **keep it** to preserve
  > behavior: re-add the `register first` check using a service `me`/`register`
  > lookup OR keep it bot-local. Simplest no-behavior-change option: keep the
  > local `get_user` guard by importing `user_db.get_user`. Decide and note in
  > commit.)

### Step 4.5 — relocate `/letsgo` into commands.py (MOVE)

- [ ] In `surfaces/discord_bot/commands.py`, add the `/letsgo` command relocated
  from `main.py:1261–1295` verbatim, importing `MintView` from
  `surfaces.discord_bot.views`.

### Step 4.6 — DELETE the parallel pipeline

- [ ] These are deleted by converting `main.py` to the pure shim (Step 4.7);
  confirm **none** of them were relocated:
  `mint_nft_for_user` (347), `create_nft_offer` (449),
  `generate_static_payment_link` (488), `generate_qr_code_image` (510),
  `create_payment_request_static` (530), `create_payment_request` (720),
  `wait_for_payment_via_subscription` (564), `generate_xumm_qr` (688),
  `check_payment_status` (728), `get_trait_files` (243), `get_random_trait`
  (259), `_rarity_pick_for_legacy` (280), `get_sorted_trait_layers` (290),
  `convert_str_to_hex` (334), `format_trait_name` (339), the inline
  FFmpeg/CDN/`record_nft_mint` block (mint_button body), and the
  `mint_nft_for_user`/`record_nft_mint`/`get_next_nft_number` usage.
- [ ] In `surfaces/discord_bot/config.py`, **remove** the now-dead constants and
  their `_require` calls that only the deleted pipeline used: `SEED` (unless
  still referenced by `admin.burn_nft` — it IS, so KEEP `SEED`), `BUNNY_CDN_*`,
  `NFT_*`, `METADATA_TEMPLATE`, `TRAIT_LAYERS_DIR`. **KEEP** `SEED`,
  `TOKEN_ISSUER_ADDRESS`, `TOKEN_CURRENCY_HEX`, `TOKEN_TRUSTLINE_LIMIT` (admin +
  trustline use them). Drop the ffmpeg `shutil.which` guard entirely.
  > **Re-check `admin.burn_nft`** still imports `SEED`/`JSON_RPC_URL` from
  > config — keep those. If you decide to leave `SEED` only in `config.py`,
  > `admin.py` reads `config.SEED`; ensure it remains defined.

### Step 4.7 — `main.py` → pure shim (delete legacy)

- [ ] Replace **all** of `main.py` with the Task 1 shim:
  ```python
  # main.py — launch shim. The Discord bot lives in surfaces/discord_bot/.
  # pm2 runs `python main.py`; this keeps that entrypoint working unchanged.
  from surfaces.discord_bot.bot import main

  if __name__ == "__main__":
      main()
  ```
- [ ] Update `bot.py`'s side-effect import block to also import `views` (so the
  buttons register) — order: `config` → `admin` → `views` → `commands`.

### Step 4.8 — mint adapter test (write COMPLETE)

- [ ] `tests/test_discord_mint.py` — drive `handle_mint(fake_svc, fake_ix)`:
  - happy path: `start_mint` → `qr_png` → `wait_for_mint` returns
    `{"state":"offer_ready", ...}` → assert two `followup.send`s (payment, then
    offer) and the offer embed title.
  - `start_mint` raises `BadRequest("no wallet", code="no_wallet")` → assert one
    `error_embed` with "register".
  - `wait_for_mint` returns `{"state":"payment_timeout"}` → assert timeout embed.
  - `start_mint` raises 409-ish → assert "in progress" message.
  ```python
  import asyncio
  from unittest.mock import AsyncMock, MagicMock

  import pytest

  from surfaces._client.errors import BadRequest, ServiceError


  def _run(coro):
      loop = asyncio.new_event_loop()
      try:
          return loop.run_until_complete(coro)
      finally:
          loop.close()


  @pytest.fixture
  def mint_mod(monkeypatch):
      for k, v in {
          "DISCORD_BOT_TOKEN": "t", "ADMIN_LOG_CHANNEL_ID": "1",
          "LFG_SERVICE_URL": "http://svc", "SERVICE_TOKEN_DISCORD": "s",
          "XUMM_API_KEY": "k", "XUMM_API_SECRET": "s",
          "TOKEN_ISSUER_ADDRESS": "rI", "TOKEN_CURRENCY_HEX": "ABC",
      }.items():
          monkeypatch.setenv(k, v)
      import importlib
      import surfaces.discord_bot.config as cfg
      importlib.reload(cfg)
      import surfaces.discord_bot.mint_view as mv
      importlib.reload(mv)
      return mv


  def _ix():
      ix = MagicMock()
      ix.user.id = 7
      ix.user.__str__ = lambda self: "bob#0002"
      ix.response.defer = AsyncMock()
      ix.followup.send = AsyncMock()
      return ix


  def _svc():
      svc = MagicMock()
      svc.start_mint = AsyncMock(return_value={"session_id": "sid", "payment_link": "L", "state": "awaiting_payment"})
      svc.qr_png = AsyncMock(return_value=b"\x89PNG")
      svc.wait_for_mint = AsyncMock(return_value={
          "session_id": "sid", "state": "offer_ready",
          "nft_number": 3600, "image_url": "https://cdn/x.png",
          "accept_url": "https://xumm/accept",
      })
      return svc


  def test_mint_happy_path(mint_mod):
      svc, ix = _svc(), _ix()
      _run(mint_mod.handle_mint(svc, ix))
      svc.start_mint.assert_awaited_once_with("7", username="bob#0002")
      svc.wait_for_mint.assert_awaited_once_with("7", "sid")
      assert ix.followup.send.await_count == 2
      offer_embed = ix.followup.send.await_args_list[1].kwargs["embed"]
      assert "Minted Successfully" in offer_embed.title


  def test_mint_no_wallet_maps_to_register(mint_mod):
      svc, ix = _svc(), _ix()
      svc.start_mint = AsyncMock(side_effect=BadRequest("no wallet", code="no_wallet", status=400))
      _run(mint_mod.handle_mint(svc, ix))
      assert ix.followup.send.await_count == 1
      embed = ix.followup.send.await_args.kwargs["embed"]
      assert "register" in embed.description.lower()


  def test_mint_payment_timeout(mint_mod):
      svc, ix = _svc(), _ix()
      svc.wait_for_mint = AsyncMock(return_value={"session_id": "sid", "state": "payment_timeout"})
      _run(mint_mod.handle_mint(svc, ix))
      embed = ix.followup.send.await_args.kwargs["embed"]
      assert "timed out" in embed.description.lower()


  def test_mint_already_in_progress(mint_mod):
      svc, ix = _svc(), _ix()
      svc.start_mint = AsyncMock(side_effect=ServiceError("mint already in progress", status=409))
      _run(mint_mod.handle_mint(svc, ix))
      embed = ix.followup.send.await_args.kwargs["embed"]
      assert "in progress" in embed.description.lower()
  ```

### Step 4.9 — gate

- [ ] Run: `.venv/bin/python -m pytest tests/test_discord_mint.py -q`
- [ ] `.venv/bin/pre-commit run --files surfaces/discord_bot/render.py surfaces/discord_bot/mint_view.py surfaces/discord_bot/views.py surfaces/discord_bot/commands.py surfaces/discord_bot/bot.py surfaces/discord_bot/config.py main.py tests/test_discord_mint.py`
- [ ] **Commit:** `feat(discord): invert Mint button onto lfg_service; delete parallel pipeline`

---

## Task 5 — Events background task → admin-log + minter DM

Launch a cancellable background task at startup running
`async for ev in svc.events(types=["mint.completed","mint.failed"])`, posting to
`ADMIN_LOG_CHANNEL_ID` and optionally DMing the minter. Cancel + `aclose()` on
shutdown per the SDK lifecycle contract.

**Files**
- NEW `surfaces/discord_bot/events.py`
- EDIT `surfaces/discord_bot/bot.py` (start task in `setup_hook`, cancel in `cleanup`)
- NEW `tests/test_discord_events.py`

**Interfaces**
- `events.py` exposes
  `async def run_event_loop(svc, get_channel, get_user) -> None` (the consumer)
  and `def make_announcement(ev) -> str` (pure formatter, unit-testable).

### Step 5.1 — events.py (write COMPLETE)

- [ ] Create `surfaces/discord_bot/events.py`:
  ```python
  # surfaces/discord_bot/events.py
  # Background firehose consumer: announces mint.completed / mint.failed to the
  # admin-log channel and optionally DMs the minter. Additive to the interactive
  # wait_for_mint path (spec D4).
  import logging
  from collections.abc import Awaitable, Callable

  import discord

  from lfg_service.events import Event
  from surfaces._client import LFGServiceClient


  def make_announcement(ev: Event) -> str:
      data = ev.data or {}
      number = data.get("nft_number", "?")
      identity = ev.identity or {}
      uid = identity.get("platform_user_id")
      who = f"<@{uid}>" if uid else "a user"
      if ev.type == "mint.completed":
          return f"🎨 NFT #{number} minted for {who}."
      return f"❌ Mint failed for {who} (#{number})."


  async def run_event_loop(
      svc: LFGServiceClient,
      announce: Callable[[str], Awaitable[None]],
      dm_user: Callable[[str, str], Awaitable[None]] | None = None,
  ) -> None:
      """Consume the service firehose forever. Reconnects internally; cancel the
      enclosing task (and the SDK aclose()s) to stop."""
      agen = svc.events(types=["mint.completed", "mint.failed"])
      try:
          async for ev in agen:
              try:
                  await announce(make_announcement(ev))
                  if dm_user is not None:
                      uid = (ev.identity or {}).get("platform_user_id")
                      if uid and ev.type == "mint.completed":
                          await dm_user(uid, make_announcement(ev))
              except Exception as e:  # never let one bad event kill the loop
                  logging.error(f"event handler error: {e}")
      finally:
          await agen.aclose()
  ```

### Step 5.2 — start/stop in bot.py

- [ ] In `bot.py`, define the announce/DM closures (resolve the channel via
  `bot.get_channel(config.ADMIN_LOG_CHANNEL_ID)`, DM via `bot.fetch_user(int(uid))`)
  and store the task handle:
  ```python
  _events_task: asyncio.Task | None = None

  @bot.event
  async def setup_hook() -> None:
      global _events_task
      await svc.__aenter__()

      async def _announce(msg: str) -> None:
          ch = bot.get_channel(config.ADMIN_LOG_CHANNEL_ID)
          if isinstance(ch, discord.TextChannel):
              await ch.send(msg)

      async def _dm(uid: str, msg: str) -> None:
          try:
              user = await bot.fetch_user(int(uid))
              await user.send(msg)
          except Exception as e:
              logging.warning(f"DM to {uid} failed: {e}")

      from surfaces.discord_bot.events import run_event_loop
      _events_task = asyncio.create_task(run_event_loop(svc, _announce, _dm))
  ```
- [ ] In `cleanup()`, cancel + await the task before closing svc:
  ```python
  global _events_task
  if _events_task is not None:
      _events_task.cancel()
      await asyncio.gather(_events_task, return_exceptions=True)
      _events_task = None
  ```
  (cancel task **before** `svc.close()` so `aclose()` can run on the live session.)

### Step 5.3 — events test (write COMPLETE)

- [ ] `tests/test_discord_events.py` — feed a hand fake `svc.events` async
  generator + capture announcements; assert formatting and aclose:
  ```python
  import asyncio

  import pytest

  from lfg_service.events import Event


  def _run(coro):
      loop = asyncio.new_event_loop()
      try:
          return loop.run_until_complete(coro)
      finally:
          loop.close()


  @pytest.fixture
  def ev_mod(monkeypatch):
      for k, v in {
          "DISCORD_BOT_TOKEN": "t", "ADMIN_LOG_CHANNEL_ID": "1",
          "LFG_SERVICE_URL": "http://svc", "SERVICE_TOKEN_DISCORD": "s",
          "XUMM_API_KEY": "k", "XUMM_API_SECRET": "s",
          "TOKEN_ISSUER_ADDRESS": "rI", "TOKEN_CURRENCY_HEX": "ABC",
      }.items():
          monkeypatch.setenv(k, v)
      import importlib
      import surfaces.discord_bot.config as cfg
      importlib.reload(cfg)
      import surfaces.discord_bot.events as ev
      importlib.reload(ev)
      return ev


  def test_make_announcement_completed(ev_mod):
      e = Event(type="mint.completed", ts=0,
                identity={"platform_user_id": "42"}, wallet=None,
                data={"nft_number": 3600})
      msg = ev_mod.make_announcement(e)
      assert "3600" in msg and "<@42>" in msg


  def test_run_event_loop_announces_and_closes(ev_mod):
      closed = {"v": False}

      class FakeAgen:
          def __init__(self):
              self._items = [
                  Event(type="mint.completed", ts=0,
                        identity={"platform_user_id": "42"}, wallet=None,
                        data={"nft_number": 3600}),
              ]
          def __aiter__(self):
              return self
          async def __anext__(self):
              if self._items:
                  return self._items.pop(0)
              raise StopAsyncIteration
          async def aclose(self):
              closed["v"] = True

      class FakeSvc:
          def events(self, types=None):
              return FakeAgen()

      sent = []
      dmed = []

      async def announce(m): sent.append(m)
      async def dm(uid, m): dmed.append((uid, m))

      _run(ev_mod.run_event_loop(FakeSvc(), announce, dm))
      assert sent and "3600" in sent[0]
      assert dmed == [("42", sent[0])]
      assert closed["v"] is True
  ```

### Step 5.4 — gate

- [ ] Run: `.venv/bin/python -m pytest tests/test_discord_events.py -q`
- [ ] `.venv/bin/pre-commit run --files surfaces/discord_bot/events.py surfaces/discord_bot/bot.py tests/test_discord_events.py`
- [ ] **Commit:** `feat(discord): background events task -> admin-log + minter DM`

---

## Task 6 — SourceTag verification, full adapter suite, pm2 shim verification

Final task: a focused SourceTag-invariant test for the bot's remaining inline tx
(trustline + admin burn), confirm the full new suite is green, and verify the
pm2 entrypoint still works.

**Files**
- NEW `tests/test_discord_sourcetag_invariant.py`
- (no production changes expected; if a gap is found, fix it here)

### Step 6.1 — SourceTag invariant test (write COMPLETE)

- [ ] `tests/test_discord_sourcetag_invariant.py` — assert the **only** two
  inline XRPL/XUMM constructions the bot still owns carry the tag:
  - trustline `TrustSet` payload (already covered in Task 2 — re-assert here as
    the canonical invariant test, importing `2606160021` from
    `lfg_core.config.SOURCE_TAG`).
  - admin `burn_nft` `NFTokenBurn` carries `source_tag=SOURCE_TAG`. Test by
    monkeypatching `submit_and_wait` to capture the tx and asserting
    `tx.source_tag == SOURCE_TAG` (or building the `NFTokenBurn` and checking
    `.source_tag`). Mock `Wallet.from_seed`/`JsonRpcClient` so no network.
  ```python
  # asserts: every tx the bot still builds inline stamps the Make Waves tag.
  from lfg_core.config import SOURCE_TAG

  def test_source_tag_constant_is_make_waves():
      assert SOURCE_TAG == 2606160021
  # + the trustline + burn assertions described above
  ```
  > **Judgment call:** mint/offer/accept are no longer built by the bot (the
  > service stamps them via `lfg_core.xrpl_ops`/`xumm_ops`, already covered by
  > `tests/test_xrpl_source_tag.py` + `tests/test_xumm_source_tag.py`). This
  > test's job is to confirm the bot has **no unstamped inline tx left** — i.e.
  > only trustline + burn remain, and both stamp. If a future inline tx is
  > added, this is where it gets caught.

### Step 6.2 — full suite + shim verification

- [ ] Run the whole new suite:
  `.venv/bin/python -m pytest tests/test_discord_*.py -q`
- [ ] Run the SDK suite to confirm no regression in consumed contract:
  `.venv/bin/python -m pytest tests/test_sdk_*.py -q`
- [ ] **pm2 shim verification (do NOT restart pm2):** confirm `python main.py`
  resolves to the package entrypoint without booting Discord, e.g.
  `.venv/bin/python -c "import ast,sys; ast.parse(open('main.py').read()); from surfaces.discord_bot.bot import main; print(callable(main))"`
  with the required env set (or via a tiny pytest that imports the shim). Confirm
  `main.py` is the thin shim (no remaining pipeline code). **Do not** run
  `bot.run` and **do not** `pm2 restart`.
- [ ] Full mypy gate over the package:
  `.venv/bin/mypy surfaces/discord_bot main.py`
- [ ] `.venv/bin/pre-commit run --files tests/test_discord_sourcetag_invariant.py`
- [ ] **Commit:** `test(discord): SourceTag invariant + adapter suite; verify pm2 shim`

---

## Self-Review

Before opening the PR, verify each item:

- [ ] **pm2 entrypoint unchanged.** `python main.py` still launches the bot;
      `main.py` is a 4-line shim re-exporting `surfaces.discord_bot.bot:main`.
      No `pm2 restart` was run by the implementer.
- [ ] **No behavior change before Task 3.** Tasks 1–2 are pure relocation; diff
      the relocated admin/trustline code against the originals (only import lines
      + the added `SourceTag` differ).
- [ ] **SourceTag invariant holds.** Trustline `TrustSet` and admin
      `NFTokenBurn` both stamp `2606160021`; mint/offer/accept go through the
      service (stamped there). No unstamped inline tx remains in
      `surfaces/discord_bot/`. Grep:
      `grep -rn "TransactionType\|NFToken\|TrustSet" surfaces/discord_bot` → every
      hit is either a service call or carries the tag.
- [ ] **Deleted pipeline is gone.** `grep -rn "mint_nft_for_user\|create_payment_request\|wait_for_payment_via_subscription\|get_sorted_trait_layers\|ffmpeg\|BunnyCDN\|record_nft_mint" surfaces/discord_bot main.py`
      returns nothing (admin's `_rarity`/sqlite usage is fine).
- [ ] **`trait_layers/` still on disk** (D5) — directory untouched.
- [ ] **Tests are sync, no pytest-asyncio, no network.** Adapter tests use a hand
      fake `svc` (MagicMock + AsyncMock) and mocked interactions; only the
      trustline test mocks `requests.post`. No `TestServer`/aiohttp in the new
      discord tests.
- [ ] **mypy override added** for `surfaces.discord_bot.*` (relaxed block);
      `surfaces._client.*` strictness untouched.
- [ ] **Client lifecycle correct.** One `svc` constructed in `bot.py`, entered in
      `setup_hook`, events task cancelled then `svc.close()` in `cleanup` (task
      cancelled BEFORE close so `aclose()` runs on a live session).
- [ ] **Every task committed green** with `pre-commit run --files <changed>`
      passing (ruff, ruff-format, mypy, gitleaks, pytest).
- [ ] **Each task is independently reviewable** — a reviewer can gate Task N's
      commit without Task N+1.

## Manual E2E (post-merge of #76/#77, on testnet — separate session)

Not part of the automated gate; run after deploy on a testnet bot instance:
`/register`, `/letsgo` full mint, trustline, one admin op — confirm the Discord
mint produces art **identical** to the web Activity (same pipeline) and the mint
txns carry the SourceTag (check on the explorer).
