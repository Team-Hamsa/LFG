#!/usr/bin/env python3
"""CI / pre-commit gate: structural + store-consistency validation of
trait_config.yaml. Exit 1 on errors; warnings are informational.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import trait_config  # noqa: E402
from lfg_core.layer_store import LocalLayerStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=trait_config.DEFAULT_CONFIG_PATH)
    p.add_argument("--layers-dir", default="layers")
    args = p.parse_args(argv)
    try:
        cfg = trait_config.load_config(args.config)
    except trait_config.TraitConfigError as e:
        print(f"ERROR: {e}")
        return 1
    error_list, warning_list = asyncio.run(
        trait_config.validate_against_store(cfg, LocalLayerStore(args.layers_dir))
    )
    for w in warning_list:
        print(f"warning: {w}")
    for err in error_list:
        print(f"ERROR: {err}")
    print(f"{len(error_list)} error(s), {len(warning_list)} warning(s)")
    return 1 if error_list else 0


if __name__ == "__main__":
    raise SystemExit(main())
