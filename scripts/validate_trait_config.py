#!/usr/bin/env python3
"""CI / pre-commit gate: structural + store-consistency validation of
trait_config.yaml. Exit 1 on errors; warnings are informational.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Env-guard: importing lfg_core.layer_store pulls in lfg_core.config, which
# calls _require() at module load time and crashes without these vars. A
# fresh checkout (or the local pre-push hook, which runs this script
# directly rather than via pytest) has no .env, so supply the same dummy
# values tests/test_seasons.py uses (copied verbatim, same keys/values) —
# this is CI-only-safe validation, not real credentials.
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import yaml  # noqa: E402

from lfg_core import trait_config  # noqa: E402
from lfg_core.layer_store import LocalLayerStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=trait_config.DEFAULT_CONFIG_PATH)
    p.add_argument("--layers-dir", default="layers")
    args = p.parse_args(argv)
    try:
        cfg = trait_config.load_config(args.config)
    except (FileNotFoundError, trait_config.TraitConfigError, yaml.YAMLError) as e:
        print(f"ERROR: {e}")
        return 1
    try:
        error_list, warning_list = asyncio.run(
            trait_config.validate_against_store(cfg, LocalLayerStore(args.layers_dir))
        )
    except OSError as e:
        print(f"ERROR: could not read layers dir {args.layers_dir!r}: {e}")
        return 1
    for w in warning_list:
        print(f"warning: {w}")
    for err in error_list:
        print(f"ERROR: {err}")
    print(f"{len(error_list)} error(s), {len(warning_list)} warning(s)")
    return 1 if error_list else 0


if __name__ == "__main__":
    raise SystemExit(main())
