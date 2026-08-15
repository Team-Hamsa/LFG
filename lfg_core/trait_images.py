# lfg_core/trait_images.py
# The single trait-preview URL resolver, shared by the real service handlers
# (lfg_service/app.py) and the dev-mode market mock (webapp/mock_market.py).
# Lives in lfg_core because app.py imports mock_market — the mock cannot
# import the resolver back out of app.py without a cycle.
from urllib.parse import quote as urlquote

from lfg_core import layer_store, trait_config


def trait_image_url(cfg: trait_config.TraitConfig, slot: str, value: str) -> str | None:
    """A same-origin /api/layer URL for a trait value, picking a representative
    body. With a local layer store the pick is disk-verified
    (LocalLayerStore.find_display_body: affinity-allowed bodies, then
    shared/, then any body with the art — display-only, so an
    affinity-illegal body's art is fine); a miss means the value has no art
    anywhere on disk, so return None rather than a URL that is known to 404.
    With a CDN store (no cheap existence probe) it falls back to the
    affinity-only guess: first allowed body, or shared/ for an unrestricted
    value."""
    allowed = cfg.allowed_bodies(slot, value)
    preferred = sorted(allowed) if allowed else []
    store = layer_store.get_layer_store()
    if isinstance(store, layer_store.LocalLayerStore):
        # Affinity alone can't pick a servable dir: an unrestricted value
        # usually lives in per-body dirs (not shared/), and a restricted
        # value's first allowed body may lack the file. Probe the disk so
        # the URL points at art that actually resolves — and admit a miss
        # (stale/renamed value) as None instead of emitting a guaranteed 404.
        body = store.find_display_body(slot, value, preferred)
        if body is None:
            return None
    else:
        body = preferred[0] if preferred else layer_store.SHARED_DIR
    return (
        f"/api/layer?body={urlquote(body, safe='')}"
        f"&trait={urlquote(slot, safe='')}&value={urlquote(value, safe='')}"
        "&thumb=1"
    )
