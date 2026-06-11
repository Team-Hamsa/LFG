#!/usr/bin/env python3
"""Upload the canonical layer tree (layers/<gender>/<TraitType>/<file>) to
BunnyCDN storage under LAYERS_CDN_FOLDER. Idempotent: skips files that
already exist with the same size."""
import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv

load_dotenv()
BASE = (os.getenv("BUNNY_CDN_BASE_URL") or "").rstrip("/")
ZONE = os.getenv("BUNNY_CDN_STORAGE_ZONE")
KEY = os.getenv("BUNNY_CDN_ACCESS_KEY")
FOLDER = os.getenv("LAYERS_CDN_FOLDER", "layers")
ROOT = sys.argv[1] if len(sys.argv) > 1 else "layers"

SEM = asyncio.Semaphore(8)


async def existing_sizes(session):
    """Map of remote rel_path -> size for everything already under FOLDER."""
    sizes = {}

    async def walk(rel):
        url = f"{BASE}/{ZONE}/{FOLDER}/{rel}" if rel else f"{BASE}/{ZONE}/{FOLDER}/"
        async with session.get(url, headers={"AccessKey": KEY}) as r:
            if r.status != 200:
                return
            for item in await r.json():
                name = item["ObjectName"]
                child = f"{rel}{name}"
                if item["IsDirectory"]:
                    await walk(child + "/")
                else:
                    sizes[child] = item["Length"]

    await walk("")
    return sizes


async def upload(session, local_path, rel_path):
    async with SEM:
        with open(local_path, "rb") as f:
            data = f.read()
        url = f"{BASE}/{ZONE}/{FOLDER}/{rel_path}"
        for attempt in range(3):
            async with session.put(url, data=data, headers={"AccessKey": KEY}) as r:
                if r.status in (200, 201):
                    return rel_path, True
            await asyncio.sleep(2 ** attempt)
        return rel_path, False


async def main():
    files = []
    for dirpath, _, names in os.walk(ROOT):
        for n in names:
            p = os.path.join(dirpath, n)
            files.append((p, os.path.relpath(p, ROOT).replace(os.sep, "/")))
    async with aiohttp.ClientSession() as session:
        done = await existing_sizes(session)
        todo = [(p, r) for p, r in files if done.get(r) != os.path.getsize(p)]
        print(f"{len(files)} local files, {len(files) - len(todo)} already on CDN, uploading {len(todo)}")
        failed = []
        for i in range(0, len(todo), 50):
            batch = todo[i:i + 50]
            results = await asyncio.gather(*(upload(session, p, r) for p, r in batch))
            failed += [r for r, ok in results if not ok]
            print(f"progress: {min(i + 50, len(todo))}/{len(todo)}", flush=True)
        if failed:
            print("FAILED:", *failed, sep="\n  ")
            sys.exit(1)
        print("all uploads ok")


asyncio.run(main())
