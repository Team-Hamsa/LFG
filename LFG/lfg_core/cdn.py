# lfg_core/cdn.py
# Shared BunnyCDN storage upload used by the mint and swap flows.

import aiohttp

from lfg_core import config


async def upload_to_bunny(folder: str, path_on_cdn: str, data: bytes,
                          content_type: str) -> str:
    """PUT bytes to BunnyCDN storage under `folder`; returns the public URL."""
    storage_url = (f"{config.BUNNY_CDN_BASE_URL}/{config.BUNNY_CDN_STORAGE_ZONE}/"
                   f"{folder}/{path_on_cdn}")
    headers = {"AccessKey": config.BUNNY_CDN_ACCESS_KEY, "Content-Type": content_type}
    async with aiohttp.ClientSession() as session:
        resp = await session.put(storage_url, headers=headers, data=data)
        if resp.status not in (200, 201):
            raise Exception(f"BunnyCDN upload failed ({resp.status}) for {path_on_cdn}")
    return f"{config.BUNNY_CDN_PUBLIC_BASE}/{folder}/{path_on_cdn}"
