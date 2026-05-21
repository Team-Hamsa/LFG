"""Generate 1000 (seed, point) test vectors using the donna C binary.

Pipeline (matches XRPL/xrpl-py):
  seed (16B) -> SHA-512(seed)[:32] -> ed25519_publickey -> point (32B)

Output: tests/data/ed25519_vectors.json
"""
import json
import secrets
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BINARY = REPO / "tools" / "dump_ed25519_vectors"
OUT = REPO / "tests" / "data" / "ed25519_vectors.json"
N = 1000


def main() -> None:
    if not BINARY.exists():
        sys.exit(f"build first: cd {BINARY.parent} && make")

    seeds = [secrets.token_bytes(16) for _ in range(N)]
    stdin = b"".join(seeds)
    proc = subprocess.run([str(BINARY)], input=stdin, capture_output=True, check=True)
    if len(proc.stdout) != 32 * N:
        sys.exit(f"expected {32 * N} stdout bytes, got {len(proc.stdout)}")

    vectors = [
        {
            "seed_hex": seeds[i].hex(),
            "point_hex": proc.stdout[i * 32 : (i + 1) * 32].hex(),
        }
        for i in range(N)
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, indent=2))
    print(f"wrote {N} vectors to {OUT}")


if __name__ == "__main__":
    main()
