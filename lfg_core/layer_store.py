# lfg_core/layer_store.py
# Unified trait-layer source shared by the mint and swap flows.
#
# Canonical structure (one tree, locally or on BunnyCDN storage):
#   <body>/<TraitType>/<Value>.png|.gif|.mp4
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

LAYER_EXTENSIONS = (".png", ".gif", ".mp4")

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
            if os.path.isdir(os.path.join(self.base_dir, d)) and not d.startswith(".")
        )

    async def list_trait_types(self, body: str) -> list[str]:
        path = os.path.join(self.base_dir, body)
        return sorted(
            d
            for d in os.listdir(path)
            if os.path.isdir(os.path.join(path, d)) and not d.startswith(".")
        )

    async def list_values(self, body: str, trait_type: str) -> list[str]:
        path = os.path.join(self.base_dir, body, trait_type)
        if not os.path.isdir(path):
            return []
        values = []
        for f in sorted(os.listdir(path)):
            stem, ext = os.path.splitext(f)
            if ext.lower() in LAYER_EXTENSIONS and not f.startswith("."):
                values.append(stem)
        return sorted(set(values))

    async def resolve(self, body: str, trait_type: str, value: str) -> str | None:
        """Local path of a layer file, or None if it doesn't exist."""
        base = os.path.join(self.base_dir, body, trait_type, value)
        for ext in LAYER_EXTENSIONS:
            if os.path.isfile(base + ext):
                return base + ext
        return None

    async def resolve_asset(self, rel_path: str) -> str | None:
        """Local path of an arbitrary file under the layer root (e.g.
        'ape/Nose.png'), or None if it doesn't exist."""
        path = os.path.join(self.base_dir, rel_path)
        return path if os.path.isfile(path) else None


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
                if resp.status != 200:
                    raise Exception(f"CDN listing failed ({resp.status}) for {rel_path or '/'}")
                items = await resp.json()
        listing = [(item["ObjectName"], bool(item.get("IsDirectory"))) for item in items]
        self._listings[rel_path] = listing
        return listing

    async def list_bodies(self) -> list[str]:
        return sorted(name for name, is_dir in await self._list_dir("") if is_dir)

    async def list_trait_types(self, body: str) -> list[str]:
        return sorted(name for name, is_dir in await self._list_dir(body) if is_dir)

    async def list_values(self, body: str, trait_type: str) -> list[str]:
        values: set[str] = set()
        for name, is_dir in await self._list_dir(f"{body}/{trait_type}"):
            if is_dir:
                continue
            stem, ext = os.path.splitext(name)
            if ext.lower() in LAYER_EXTENSIONS:
                values.add(stem)
        return sorted(values)

    async def resolve(self, body: str, trait_type: str, value: str) -> str | None:
        """Download (or reuse cached) layer file; returns local path or None."""
        listing = await self._list_dir(f"{body}/{trait_type}")
        names = {name for name, is_dir in listing if not is_dir}
        for ext in LAYER_EXTENSIONS:
            filename = value + ext
            if filename in names:
                return await self._download(f"{body}/{trait_type}/{filename}")
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
