# LFG Bot Consolidation & Bug Fix Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all duplicate/stale files from the LFG repo and fix five critical bugs in the authoritative `main.py`.

**Architecture:** The authoritative codebase lives at `/home/hamsa/LFG/` root. The `lfg-discord-mint-bot/` subdirectory is a stale Sep-2025 snapshot and `backup/` contains historical snapshots — both are deleted. Five bugs in `main.py` are fixed in-place with no new files or abstractions.

**Tech Stack:** Python 3.x, discord.py, xrpl-py, xumm-sdk-py, SQLite, BunnyCDN, aiohttp

---

## File Map

Files **deleted** (stale duplicates):
- `lfg-discord-mint-bot/` — entire subdirectory (older Sep-2025 snapshot)
- `backup/` — all 8 historical main.py snapshots
- `updated-mint-bot.py` — intermediate draft
- `main backup working.py` — old working copy without admin feature
- `metadata.json` — leftover dev artifact with wrong storage URL
- `metadata_1738694140.json`, `metadata_1738694558.json`, `metadata_1738694675.json` — test mint leftovers

Files **modified** (bug fixes):
- `main.py` — 5 bugs fixed (see tasks 2–6)

Files **unchanged** (already authoritative):
- `db_helpers.py`, `user_db.py`, `ts_helpers.py`, `init_db.py`, `requirements.txt`, `setup.sh`, `CLAUDE.md`, `README.md`, `trait_layers/`, `lfg_nfts.db`, `users.json`

---

## Task 1: Delete stale files and directories

**Files:** delete only

- [ ] **Step 1: Delete the lfg-discord-mint-bot subdirectory**

```bash
rm -rf /home/hamsa/LFG/lfg-discord-mint-bot
```

- [ ] **Step 2: Delete the backup directory**

```bash
rm -rf /home/hamsa/LFG/backup
```

- [ ] **Step 3: Delete stale root-level duplicate files**

```bash
rm /home/hamsa/LFG/updated-mint-bot.py
rm "/home/hamsa/LFG/main backup working.py"
rm /home/hamsa/LFG/metadata.json
rm /home/hamsa/LFG/metadata_1738694140.json
rm /home/hamsa/LFG/metadata_1738694558.json
rm /home/hamsa/LFG/metadata_1738694675.json
```

- [ ] **Step 4: Verify only clean files remain**

```bash
ls /home/hamsa/LFG/
```

Expected output contains only: `CLAUDE.md  db_helpers.py  docs  init_db.py  lfg_nfts.db  lfgnfts.json  main.py  MintBot  nftnv  __pycache__  README.md  requirements.txt  setup.sh  trait_layers  ts_helpers.py  updated-mint-bot.py  user_db.py  users.json` — and specifically does NOT contain `backup/`, `lfg-discord-mint-bot/`, `metadata*.json`, `updated-mint-bot.py`, or `main backup working.py`.

- [ ] **Step 5: Commit**

```bash
git -C /home/hamsa/LFG add -A
git -C /home/hamsa/LFG commit -m "chore: remove stale duplicate files and backup snapshots"
```

---

## Task 2: Fix — get_user reads users.json instead of SQLite

**File:** `main.py`

**Bug:** `get_user()` at line 254 reads the legacy `users.json` file. The `/register` slash command stores users in the SQLite `Users` table via `user_db.register_user()`. A user who registered via `/register` will always get "Please register your wallet first" because `get_user()` never checks the DB.

`user_db.py` already exports a correct `get_user(discord_id: str)` function that reads from SQLite and returns `{"id": ..., "address": ..., "name": ...}` — the same shape the callers expect.

- [ ] **Step 1: Update the import at the top of main.py**

Find the line (around line 37):
```python
from user_db import register_user, create_users_table
```
Replace with:
```python
from user_db import register_user, create_users_table, get_user as get_user_from_db
```

- [ ] **Step 2: Replace the get_user function definition**

Find and replace the entire `get_user` function (lines ~254-268):

Old:
```python
def get_user(user):
    try:
        with open("users.json", "r") as f:
            data = json.load(f)
            users = data.get("users", [])  # Get users list or empty list if not found
            
        for userr in users:
            if userr["id"] == str(user.id):
                return userr
        return None
    except (FileNotFoundError, json.JSONDecodeError):
        # If file doesn't exist or is invalid, create it with empty users list
        with open("users.json", "w") as f:
            json.dump({"users": []}, f, indent=2)
        return None
```

New:
```python
def get_user(user):
    return get_user_from_db(str(user.id))
```

- [ ] **Step 3: Verify call sites still work**

The three call sites are:
- Line ~806: `user_data = get_user(interaction.user)` → checks `user_data.get("address")`
- Line ~1029: `user_data = get_user(interaction.user)` → checks `user_data.get("address")`
- Line ~1107: `user_data = get_user(interaction.user)` → checks `user_data.get("address")`

`user_db.get_user()` returns `{"id": ..., "address": ..., "name": ...}` so `user_data.get("address")` works correctly. No call-site changes needed.

- [ ] **Step 4: Commit**

```bash
git -C /home/hamsa/LFG add main.py
git -C /home/hamsa/LFG commit -m "fix: get_user now reads from SQLite Users table instead of legacy users.json"
```

---

## Task 3: Fix — ADMIN_LOG_CHANNEL_ID used as string not int

**File:** `main.py`

**Bug:** `ADMIN_LOG_CHANNEL_ID = os.getenv("ADMIN_LOG_CHANNEL_ID")` loads it as a `str`. `interaction.guild.get_channel()` requires an `int`. The call silently returns `None`, so burn audit logs never post to Discord.

- [ ] **Step 1: Find and fix the ADMIN_LOG_CHANNEL_ID assignment**

Find (around line 89):
```python
ADMIN_LOG_CHANNEL_ID = os.getenv("ADMIN_LOG_CHANNEL_ID")
if not ADMIN_LOG_CHANNEL_ID:
    raise ValueError("ADMIN_LOG_CHANNEL_ID not found in environment variables")
```

Replace with:
```python
ADMIN_LOG_CHANNEL_ID = int(os.getenv("ADMIN_LOG_CHANNEL_ID", "0"))
if not ADMIN_LOG_CHANNEL_ID:
    raise ValueError("ADMIN_LOG_CHANNEL_ID not found in environment variables")
```

- [ ] **Step 2: Verify the usage site**

Search for the usage:
```bash
grep -n "get_channel(ADMIN_LOG_CHANNEL_ID)" /home/hamsa/LFG/main.py
```

Confirm there is exactly one hit, and that it now receives an `int`. No further changes needed.

- [ ] **Step 3: Commit**

```bash
git -C /home/hamsa/LFG add main.py
git -C /home/hamsa/LFG commit -m "fix: cast ADMIN_LOG_CHANNEL_ID to int so get_channel() receives correct type"
```

---

## Task 4: Fix — wrong metadata CDN URL minted on-chain

**File:** `main.py`

**Bug:** Around line 977, `metadata_cdn_url` is set to the BunnyCDN *storage* URL (`storage.bunnycdn.com/lfgo/...`) for the PUT upload. That same variable is immediately passed to `mint_nft_for_user(metadata_cdn_url=...)` before being corrected to the public CDN URL (`lfgo.b-cdn.net/...`) on line ~1004. Every NFT is minted with an on-chain URI that points to the authenticated storage endpoint instead of the public CDN.

- [ ] **Step 1: Find the upload block and separate the upload URL from the CDN URL**

Find the block (around lines 973–1000):
```python
async with aiohttp.ClientSession() as session:
    metadata_cdn_url = f"https://storage.bunnycdn.com/lfgo/minttest/{metadata_filename}"
    headers = {
        "AccessKey": BUNNY_CDN_ACCESS_KEY,
        "Content-Type": "application/json",
    }
    with open(metadata_filename, 'rb') as file:
        await session.put(metadata_cdn_url, headers=headers, data=file.read())

# Clean up local files
os.remove(combined_image_path)
os.remove(metadata_filename)

# Mint the NFT
nft_id = await mint_nft_for_user(
    metadata_cdn_url=metadata_cdn_url,
    taxon=NFT_TAXON,
    issuer=TOKEN_ISSUER_ADDRESS
)
```

Replace with:
```python
metadata_upload_url = f"https://storage.bunnycdn.com/lfgo/minttest/{metadata_filename}"
metadata_cdn_url = f"https://lfgo.b-cdn.net/minttest/{metadata_filename}"
async with aiohttp.ClientSession() as session:
    headers = {
        "AccessKey": BUNNY_CDN_ACCESS_KEY,
        "Content-Type": "application/json",
    }
    with open(metadata_filename, 'rb') as file:
        await session.put(metadata_upload_url, headers=headers, data=file.read())

# Clean up local files
os.remove(combined_image_path)
os.remove(metadata_filename)

# Mint the NFT
nft_id = await mint_nft_for_user(
    metadata_cdn_url=metadata_cdn_url,
    taxon=NFT_TAXON,
    issuer=TOKEN_ISSUER_ADDRESS
)
```

- [ ] **Step 2: Remove the now-redundant reassignment below**

Find and delete the line (around line 1004) that was re-assigning after the fact:
```python
metadata_cdn_url = f"https://lfgo.b-cdn.net/minttest/{metadata_filename}"
```
This line no longer exists because we moved it above. Confirm it's gone:
```bash
grep -n "b-cdn.net/minttest" /home/hamsa/LFG/main.py
```
Expected: exactly one hit (the new assignment before the upload).

- [ ] **Step 3: Commit**

```bash
git -C /home/hamsa/LFG add main.py
git -C /home/hamsa/LFG commit -m "fix: mint on-chain URI with public CDN URL, not authenticated storage URL"
```

---

## Task 5: Fix — METADATA_TEMPLATE defined twice (env-var version overwritten)

**File:** `main.py`

**Bug:** `METADATA_TEMPLATE` is defined twice. The first definition (around line 170) uses env-var values (`NFT_SCHEMA_URL`, `NFT_DESCRIPTION`, etc.). The second definition (around line 233) hardcodes the same values as string literals. Python silently uses the second; the first is dead code and the env-var overrides do nothing.

- [ ] **Step 1: Delete the second (hardcoded) METADATA_TEMPLATE definition**

Find the second block that looks like:
```python
# Metadata template used for generating NFT metadata
METADATA_TEMPLATE = {
    "schema": "ipfs://QmNpi8rcXEkohca8iXu7zysKKSJYqCvBJn3xJwga8jXqWU",
    "name": "",  # Will be filled in with "Let's Effing Go! #{number}"
    "description": "Test",
    "image": "",  # Will be filled with CDN URL
    "video": "",  # Empty string instead of None
    "external_link": "https://letseffinggo.com",
    "collection": {
        "name": "Let's Effing Go!",
        "family": "Test"
    },
    "edition": 0,  # Integer instead of None
    "attributes": []  # Will be filled with the traits
}
```

Delete it entirely (the first definition that uses env-vars is the one to keep).

- [ ] **Step 2: Verify only one METADATA_TEMPLATE definition remains**

```bash
grep -n "METADATA_TEMPLATE = {" /home/hamsa/LFG/main.py
```

Expected: exactly one hit.

- [ ] **Step 3: Commit**

```bash
git -C /home/hamsa/LFG add main.py
git -C /home/hamsa/LFG commit -m "fix: remove duplicate METADATA_TEMPLATE definition that shadowed env-var values"
```

---

## Task 6: Fix — burned_nfts table may not exist when stats_button queries it

**File:** `main.py`

**Bug:** The `stats_button` handler runs `SELECT COUNT(*) FROM burned_nfts` directly. The `burned_nfts` table is only created inside `BurnConfirmView.confirm_burn()` — if no burn has ever been performed, the table doesn't exist and the stats query crashes.

The fix is to move `burned_nfts` table creation to `init_db.py` so it's always created at startup.

- [ ] **Step 1: Open init_db.py and add the burned_nfts table**

Read the current `init_db.py` to find where tables are created:
```bash
cat /home/hamsa/LFG/init_db.py
```

Add the `burned_nfts` CREATE TABLE statement alongside the existing ones. The schema (from CLAUDE.md) is:
```sql
CREATE TABLE IF NOT EXISTS burned_nfts (
    nft_number INTEGER PRIMARY KEY,
    nft_id TEXT,
    discord_id TEXT,
    burned_by TEXT,
    reason TEXT,
    burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    original_mint_time TIMESTAMP
)
```

Add it to `init_db.py` using the same pattern as the existing table creations. Use `CREATE TABLE IF NOT EXISTS` so it's idempotent.

- [ ] **Step 2: Verify init_db.py creates all three tables**

```bash
grep -n "CREATE TABLE" /home/hamsa/LFG/init_db.py
```

Expected: three hits — `LFG`, `Users`, and `burned_nfts`.

- [ ] **Step 3: Also check if main.py creates burned_nfts inline and remove the redundancy**

```bash
grep -n "CREATE TABLE.*burned_nfts\|burned_nfts.*CREATE" /home/hamsa/LFG/main.py
```

If found, delete that inline CREATE TABLE block from `main.py` — `init_db.py` now owns it.

- [ ] **Step 4: Commit**

```bash
git -C /home/hamsa/LFG add init_db.py main.py
git -C /home/hamsa/LFG commit -m "fix: create burned_nfts table at startup in init_db.py to prevent stats crash"
```

---

## Final Verification

- [ ] **Confirm no duplicate files remain**

```bash
ls /home/hamsa/LFG/
# Should NOT contain: backup/, lfg-discord-mint-bot/, metadata*.json, updated-mint-bot.py, "main backup working.py"
```

- [ ] **Confirm Python syntax is clean**

```bash
python3 -m py_compile /home/hamsa/LFG/main.py && echo "OK"
python3 -m py_compile /home/hamsa/LFG/user_db.py && echo "OK"
python3 -m py_compile /home/hamsa/LFG/db_helpers.py && echo "OK"
python3 -m py_compile /home/hamsa/LFG/init_db.py && echo "OK"
```

Expected: all print `OK` with no errors.

- [ ] **Confirm METADATA_TEMPLATE uses env-var values**

```bash
grep -A5 "METADATA_TEMPLATE = {" /home/hamsa/LFG/main.py | head -10
```

The `schema`, `description`, `external_link`, and `collection.name` fields should reference variables (e.g., `NFT_SCHEMA_URL`, `NFT_DESCRIPTION`), not hardcoded strings.
