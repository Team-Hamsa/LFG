# lfg_core/layer_thumbs.py
# Path mapping + scan logic for the layer thumbnail tier (layers/.thumbs/).
#
# Full-res layer art (1080x1080 PNG / GIF / VP9-alpha WebM / MP4) is what the
# compose pipeline consumes, but it is the wrong thing to ship to preview UIs:
# WebM/MP4 don't render in <img> at all (broken tiles in the trait shop and the
# Discord Activity), and multi-MB 1080 assets are wasteful in grids. The thumb
# tier mirrors the layers tree under layers/.thumbs/ — dot-prefixed so
# LocalLayerStore.list_bodies never sees it as a body dir — with every source
# downscaled to THUMB_SIZE and every animated format re-encoded as GIF, which
# renders in a plain <img> everywhere.
#
# This module is pure path/mtime logic (no ffmpeg/gifski) so the service and
# tests can use it without media tooling; scripts/make_layer_thumbs.py does the
# actual conversion.

import os

THUMBS_DIR = ".thumbs"
THUMB_SIZE = 512
# Extensions the layer store serves (layer_store.LAYER_EXTENSIONS) and their thumb
# output format: PNGs stay PNG, every animated container becomes GIF.
_THUMB_EXT = {".png": ".png", ".gif": ".gif", ".webm": ".gif", ".mp4": ".gif"}
# Reverse map: which source extensions a given thumb can stand in for.
_SOURCES_FOR_THUMB = {
    ".png": (".png",),
    ".gif": (".gif", ".webm", ".mp4"),
}


def thumb_path_for(src_path: str, base_dir: str) -> str | None:
    """The .thumbs/ path standing in for `src_path`, or None when the file is
    outside `base_dir`, already inside .thumbs/, or not a layer format."""
    rel = os.path.relpath(os.path.abspath(src_path), os.path.abspath(base_dir))
    if rel.startswith("..") or rel.split(os.sep, 1)[0] == THUMBS_DIR:
        return None
    stem, ext = os.path.splitext(rel)
    thumb_ext = _THUMB_EXT.get(ext.lower())
    if thumb_ext is None:
        return None
    return os.path.join(base_dir, THUMBS_DIR, stem + thumb_ext)


def scan(base_dir: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Diff the layers tree against its .thumbs/ mirror.

    Returns (stale, orphans): `stale` is [(src, thumb)] pairs whose thumb is
    missing or older than its source (mtime), `orphans` is thumb files whose
    source no longer exists in any format that maps to them. Hidden dirs
    (including .thumbs itself) are never treated as sources.
    """
    stale: list[tuple[str, str]] = []
    sources: set[str] = set()
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            src = os.path.join(root, f)
            thumb = thumb_path_for(src, base_dir)
            if thumb is None:
                continue
            sources.add(src)
            try:
                fresh = os.path.getmtime(thumb) >= os.path.getmtime(src)
            except OSError:
                fresh = False
            if not fresh:
                stale.append((src, thumb))

    orphans: list[str] = []
    thumbs_root = os.path.join(base_dir, THUMBS_DIR)
    for root, dirs, files in os.walk(thumbs_root):
        dirs[:] = sorted(dirs)
        for f in sorted(files):
            thumb = os.path.join(root, f)
            rel = os.path.relpath(thumb, thumbs_root)
            stem, ext = os.path.splitext(rel)
            src_exts = _SOURCES_FOR_THUMB.get(ext.lower(), ())
            if not any(os.path.join(base_dir, stem + se) in sources for se in src_exts):
                orphans.append(thumb)
    return stale, orphans
