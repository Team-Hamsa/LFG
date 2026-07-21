# lfg_core/layer_store.py
# Unified trait-layer source shared by the mint and swap flows.
#
# Canonical structure (one tree, locally or on BunnyCDN storage):
#   <body>/<TraitType>/<Value>.png|.gif|.webm|.mp4
# e.g. male/Eyes/Laser.png  —  the file stem IS the metadata trait value.
#
# CdnLayerStore lists directories via the Bunny storage API and downloads
# layer files on demand into LAYER_CACHE_DIR (idempotent; cached files are
# reused). LocalLayerStore serves the same API from a local directory for
# development and tests. Select with LAYER_SOURCE=cdn|local.

import logging
import os

import aiohttp

from lfg_core import config

LAYER_EXTENSIONS = (".png", ".gif", ".webm", ".mp4")

# Traits under this directory are available to every body (e.g. seasonal
# cosmetics that aren't body-specific). list_values/list_trait_types union it
# in; resolve tries the body's own dir first, then falls back here. A missing
# shared/ dir (e.g. before the Task 19 migration lands) degrades to a no-op.
SHARED_DIR = "shared"

# Bunny storage can stall; never let a listing or layer download hang a
# mint/swap session forever. Downloads get longer for multi-MB video layers.
LIST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=10)


class LocalLayerStore:
    def __init__(self, base_dir: str | None = None) -> None:
        self.base_dir = base_dir or config.LAYERS_DIR

    async def list_bodies(self) -> list[str]:
        return sorted(
            d
            for d in os.listdir(self.base_dir)
            if os.path.isdir(os.path.join(self.base_dir, d))
            and not d.startswith(".")
            and d != SHARED_DIR
        )

    async def list_trait_types(self, body: str) -> list[str]:
        types = set(self._list_trait_types_one(body))
        types |= set(self._list_trait_types_one(SHARED_DIR))
        return sorted(types)

    def _list_trait_types_one(self, dirname: str) -> list[str]:
        path = os.path.join(self.base_dir, dirname)
        if not os.path.isdir(path):
            return []
        return [
            d
            for d in os.listdir(path)
            if os.path.isdir(os.path.join(path, d)) and not d.startswith(".")
        ]

    async def list_values(self, body: str, trait_type: str) -> list[str]:
        values = set(self._list_values_one(body, trait_type))
        values |= set(self._list_values_one(SHARED_DIR, trait_type))
        return sorted(values)

    def _list_values_one(self, dirname: str, trait_type: str) -> list[str]:
        path = os.path.join(self.base_dir, dirname, trait_type)
        if not os.path.isdir(path):
            return []
        values = []
        for f in sorted(os.listdir(path)):
            stem, ext = os.path.splitext(f)
            if ext.lower() in LAYER_EXTENSIONS and not f.startswith("."):
                values.append(stem)
        return sorted(set(values))

    async def resolve(self, body: str, trait_type: str, value: str) -> str | None:
        """Local path of a layer file, checking the body dir then shared/,
        or None if it doesn't exist in either."""
        for dirname in (body, SHARED_DIR):
            base = os.path.join(self.base_dir, dirname, trait_type, value)
            for ext in LAYER_EXTENSIONS:
                if os.path.isfile(base + ext):
                    return base + ext
        return None

    async def resolve_asset(self, rel_path: str) -> str | None:
        """Local path of an arbitrary file under the layer root (e.g.
        'ape/Nose.png'), or None if it doesn't exist."""
        path = os.path.join(self.base_dir, rel_path)
        return path if os.path.isfile(path) else None

    def find_display_body(
        self, trait_type: str, value: str, preferred: list[str] | None = None
    ) -> str | None:
        """Sync: first directory that actually holds art for (trait_type,
        value) — preferred bodies first, then shared/, then any other body —
        or None if the value has no art anywhere. Display-only: a
        non-preferred body may be affinity-illegal for minting, but its art
        is still the right thumbnail for a body-agnostic catalog card."""

        def has_art(dirname: str) -> bool:
            base = os.path.join(self.base_dir, dirname, trait_type, value)
            return any(os.path.isfile(base + ext) for ext in LAYER_EXTENSIONS)

        candidates = list(dict.fromkeys(list(preferred or []) + [SHARED_DIR]))
        for dirname in candidates:
            if has_art(dirname):
                return dirname
        # Only scan the layer root when the cheap candidates all miss — the
        # catalog calls this once per value, so the common (found) case must
        # not pay a base-dir listing.
        try:
            others = sorted(
                d
                for d in os.listdir(self.base_dir)
                if os.path.isdir(os.path.join(self.base_dir, d))
                and not d.startswith(".")
                and d not in candidates
            )
        except OSError:
            return None
        for dirname in others:
            if has_art(dirname):
                return dirname
        return None


class CdnListingNotFound(Exception):
    """Raised by _list_dir when the CDN reports 404 for a listed path. This
    is the ONLY failure mode callers may treat as "empty directory" — a
    body's own trait dir can legitimately 404 post shared-layers-migration
    (its values moved to shared/), same as an unmigrated/empty shared/ tree.
    Any other failure (timeout, 5xx, auth) is a real outage and must not be
    caught here — it propagates as a bare Exception."""


class CdnLayerStore:
    """BunnyCDN storage-backed layer tree with an on-disk download cache.
    Directory listings are cached in memory for the life of the instance."""

    def __init__(self) -> None:
        self.cache_dir = config.LAYER_CACHE_DIR
        self._listings: dict[str, list[tuple[str, bool]]] = {}

    def _storage_url(self, rel_path: str) -> str:
        return (
            f"{config.BUNNY_CDN_BASE_URL}/{config.BUNNY_CDN_STORAGE_ZONE}/"
            f"{config.LAYERS_CDN_FOLDER}/{rel_path}"
        )

    async def _list_dir(self, rel_path: str) -> list[tuple[str, bool]]:
        if rel_path in self._listings:
            return self._listings[rel_path]
        url = self._storage_url(rel_path)
        if not url.endswith("/"):
            url += "/"
        headers = {"AccessKey": config.BUNNY_CDN_ACCESS_KEY}
        async with aiohttp.ClientSession(timeout=LIST_TIMEOUT) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 404:
                    raise CdnListingNotFound(f"CDN listing not found for {rel_path or '/'}")
                if resp.status != 200:
                    raise Exception(f"CDN listing failed ({resp.status}) for {rel_path or '/'}")
                items = await resp.json()
        listing = [(item["ObjectName"], bool(item.get("IsDirectory"))) for item in items]
        self._listings[rel_path] = listing
        return listing

    async def _list_dir_tolerant(self, rel_path: str) -> list[tuple[str, bool]]:
        """Like _list_dir, but a missing directory (404 — no shared/
        migration yet, nothing shared for this trait type, or a body's own
        trait dir emptied by the shared-layers migration) is not an error —
        treat as empty. Real failures (timeouts, 5xx, auth) still propagate."""
        try:
            return await self._list_dir(rel_path)
        except CdnListingNotFound:
            return []

    async def list_bodies(self) -> list[str]:
        return sorted(
            name for name, is_dir in await self._list_dir("") if is_dir and name != SHARED_DIR
        )

    async def list_trait_types(self, body: str) -> list[str]:
        types = {name for name, is_dir in await self._list_dir_tolerant(body) if is_dir}
        types |= {name for name, is_dir in await self._list_dir_tolerant(SHARED_DIR) if is_dir}
        return sorted(types)

    async def list_values(self, body: str, trait_type: str) -> list[str]:
        values: set[str] = set()
        for dirname in (body, SHARED_DIR):
            for name, is_dir in await self._list_dir_tolerant(f"{dirname}/{trait_type}"):
                if is_dir:
                    continue
                stem, ext = os.path.splitext(name)
                if ext.lower() in LAYER_EXTENSIONS:
                    values.add(stem)
        return sorted(values)

    async def resolve(self, body: str, trait_type: str, value: str) -> str | None:
        """Download (or reuse cached) layer file, checking the body dir then
        shared/; returns local path or None if absent from both."""
        for dirname in (body, SHARED_DIR):
            listing = await self._list_dir_tolerant(f"{dirname}/{trait_type}")
            names = {name for name, is_dir in listing if not is_dir}
            for ext in LAYER_EXTENSIONS:
                filename = value + ext
                if filename in names:
                    return await self._download(f"{dirname}/{trait_type}/{filename}")
        return None

    async def resolve_asset(self, rel_path: str) -> str | None:
        """Download (or reuse cached) an arbitrary file under the layer root
        (e.g. 'ape/Nose.png'); returns local path or None if absent."""
        parent, _, name = rel_path.rpartition("/")
        listing = await self._list_dir(parent)
        if name in {n for n, is_dir in listing if not is_dir}:
            return await self._download(rel_path)
        return None

    async def _download(self, rel_path: str) -> str:
        local_path = os.path.join(self.cache_dir, rel_path)
        if os.path.isfile(local_path) and os.path.getsize(local_path) > 0:
            return local_path
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        headers = {"AccessKey": config.BUNNY_CDN_ACCESS_KEY}
        async with aiohttp.ClientSession(timeout=DOWNLOAD_TIMEOUT) as session:
            async with session.get(self._storage_url(rel_path), headers=headers) as resp:
                if resp.status != 200:
                    raise Exception(f"CDN download failed ({resp.status}) for {rel_path}")
                data = await resp.read()
        tmp_path = local_path + ".part"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, local_path)
        logging.info(f"Cached layer: {rel_path}")
        return local_path


_store: LocalLayerStore | CdnLayerStore | None = None


def get_layer_store() -> LocalLayerStore | CdnLayerStore:
    """Process-wide store singleton so CDN directory listings and the
    download cache survive across mint/swap sessions."""
    global _store
    if _store is None:
        _store = LocalLayerStore() if config.LAYER_SOURCE == "local" else CdnLayerStore()
    return _store
