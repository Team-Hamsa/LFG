# XRP Vanity GPU Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a CUDA-accelerated CLI (`python xrp_vanity_gpu.py PATTERN`) that finds XRPL Ed25519 addresses whose base58 representation begins with PATTERN (after the leading `r`).

**Architecture:** Hybrid GPU/CPU pipeline in one Python process. GPU runs `seed → SHA-512[:32] → Ed25519 scalar-mult-base → 33-byte compressed pubkey`. CPU runs `SHA-256 → RIPEMD-160 → base58check → prefix match`. Ed25519 kernel ported from `ed25519-donna`.

**Tech Stack:** Python 3.10+, CuPy 13.4.1 (NVRTC, no nvcc), `xrpl-py` 4.5.0 (oracle), `pycryptodome` (RIPEMD-160), `hashlib` (SHA-256), `numpy`, `pytest`. Conda env `rapids-23.12`. RTX 2060 Super, CUDA 12.2.

**Spec:** `docs/superpowers/specs/2026-05-20-xrp-vanity-gpu-design.md`.

**Running tests/CLI:** Always prefix with the conda activator:
```
source ~/miniconda3/etc/profile.d/conda.sh && conda run -n rapids-23.12 <cmd>
```

**Working directory for all paths below:** `/home/hamsa/xrp_vanity_gpu/`.

**Commit convention:** Conventional Commits (`feat:`, `fix:`, `chore:`, `test:`, `refactor:`, `docs:`). Every commit must end with the `Co-Authored-By` trailer used by the existing repo. Stage files explicitly (no `git add -A`).

**Git root caveat:** `git rev-parse --show-toplevel` returns `/home/hamsa`. Use `git -C /home/hamsa <cmd>` for git operations, and stage files with paths prefixed `xrp_vanity_gpu/...`.

---

## Task 0: Archive obsolete artifacts and slim sha_kernels.cu

**Files:**
- Create: `xrp_vanity_gpu/archive/README.md`
- Move: `xrp_vanity_gpu/kernels/base58_kernel.cu` → `xrp_vanity_gpu/archive/base58_kernel.cu`
- Move: `xrp_vanity_gpu/kernels/ripemd160_kernel.cu` → `xrp_vanity_gpu/archive/ripemd160_kernel.cu`
- Move: `xrp_vanity_gpu/debug/*` → `xrp_vanity_gpu/archive/debug/`
- Modify: `xrp_vanity_gpu/kernels/sha_kernels.cu` (strip SHA-256 device functions)

- [ ] **Step 1: Create archive/ and move obsolete kernel files**

```
mkdir -p xrp_vanity_gpu/archive/debug
git -C /home/hamsa mv xrp_vanity_gpu/kernels/base58_kernel.cu xrp_vanity_gpu/archive/base58_kernel.cu
git -C /home/hamsa mv xrp_vanity_gpu/kernels/ripemd160_kernel.cu xrp_vanity_gpu/archive/ripemd160_kernel.cu
```

- [ ] **Step 2: Move debug scripts under archive/**

```
for f in xrp_vanity_gpu/debug/*.py; do
  git -C /home/hamsa mv "$f" "xrp_vanity_gpu/archive/debug/$(basename "$f")"
done
rmdir xrp_vanity_gpu/debug
```

- [ ] **Step 3: Inspect sha_kernels.cu to identify the SHA-256 region**

Read `xrp_vanity_gpu/kernels/sha_kernels.cu` and note the line ranges of:
- The K256[] constant table
- All `__device__` SHA-256 helpers (`sha256_compress`, `sha256_pad`, etc.)
- Any `extern "C" __global__` SHA-256 kernels

Keep all SHA-512 code intact. The file should end with only SHA-512 code reachable.

- [ ] **Step 4: Strip SHA-256 from sha_kernels.cu**

Use Edit to remove every SHA-256 block. After editing, the file should contain only:
- NVRTC preamble (typedef + uint64 const macros)
- K512[] table
- `__device__` SHA-512 helpers
- `extern "C" __global__` SHA-512 launcher kernel (if any)

- [ ] **Step 5: Verify SHA-512 still compiles by re-running its existing test**

Run:
```
source ~/miniconda3/etc/profile.d/conda.sh && conda run -n rapids-23.12 \
  pytest xrp_vanity_gpu/tests/test_sha.py -v
```

Expected: SHA-512 tests PASS. If a test imports a SHA-256 symbol, mark that test `@pytest.mark.skip(reason="SHA-256 archived per design 2026-05-20")` — do not delete it.

- [ ] **Step 6: Write archive/README.md**

```markdown
# Archive

Artifacts parked from the pre-2026-05-20 GPU-only design. None of these
are built or imported by the active code. They are kept for reference
in case the GPU SHA-256 mystery is ever revisited.

## Contents

- `base58_kernel.cu` — CUDA base58 encoder. Passed all tests. Superseded
  by the hybrid design which does base58 on CPU.
- `ripemd160_kernel.cu` — CUDA RIPEMD-160. Untested. Superseded by the
  hybrid design which uses pycryptodome on CPU.
- `debug/` — SHA-256 isolation scripts from the unsolved bug
  investigation. Reproduces a deterministic wrong digest in BOTH the
  GPU kernel and every from-scratch Python rewrite. See
  `~/.claude/projects/-home-hamsa/memory/project_xrp_vanity_gpu.md`
  for the full debug history.

## Why these are parked, not deleted

The GPU SHA-256 bug is sufficiently weird (5+ independent Python
rewrites all converge on the same wrong digest while OpenSSL,
sha256sum, hashlib, and pycryptodome all agree on the correct one)
that it is plausibly still worth solving as a curiosity. The hybrid
design simply routes around it.
```

- [ ] **Step 7: Commit**

```
git -C /home/hamsa add xrp_vanity_gpu/archive/ xrp_vanity_gpu/kernels/sha_kernels.cu
git -C /home/hamsa commit -m "$(cat <<'EOF'
chore: archive base58/ripemd160 kernels, debug scripts; slim sha_kernels.cu to SHA-512

Per the hybrid GPU/CPU design, hashing and base58 move to CPU.
Park the obsolete GPU implementations under archive/ rather than
deleting, in case the unsolved GPU SHA-256 bug is ever revisited.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 1: Python package scaffolding

**Files:**
- Create: `xrp_vanity_gpu/vanity/__init__.py`
- Create: `xrp_vanity_gpu/tests/__init__.py`
- Create: `xrp_vanity_gpu/tests/conftest.py`
- Create: `xrp_vanity_gpu/tests/data/.gitkeep`
- Create: `xrp_vanity_gpu/pyproject.toml`

- [ ] **Step 1: Create vanity/ package init**

`xrp_vanity_gpu/vanity/__init__.py`:
```python
"""GPU-accelerated XRPL vanity address search."""
__all__ = ["encoding", "sieve", "stats", "gpu"]
```

- [ ] **Step 2: Create tests package init**

`xrp_vanity_gpu/tests/__init__.py`: empty file.

- [ ] **Step 3: Create conftest.py with marker definitions**

`xrp_vanity_gpu/tests/conftest.py`:
```python
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "gpu: requires a CUDA-capable GPU and CuPy at import time",
    )
    config.addinivalue_line(
        "markers",
        "slow: takes more than a few seconds (e.g. 1000-vector loops)",
    )
```

- [ ] **Step 4: Create test data placeholder**

```
mkdir -p xrp_vanity_gpu/tests/data
touch xrp_vanity_gpu/tests/data/.gitkeep
```

- [ ] **Step 5: Create pyproject.toml**

`xrp_vanity_gpu/pyproject.toml`:
```toml
[project]
name = "xrp-vanity-gpu"
version = "0.1.0"
requires-python = ">=3.10"
description = "GPU-accelerated XRPL Ed25519 vanity address search."

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra -q"
```

- [ ] **Step 6: Commit**

```
git -C /home/hamsa add \
  xrp_vanity_gpu/vanity/__init__.py \
  xrp_vanity_gpu/tests/__init__.py \
  xrp_vanity_gpu/tests/conftest.py \
  xrp_vanity_gpu/tests/data/.gitkeep \
  xrp_vanity_gpu/pyproject.toml
git -C /home/hamsa commit -m "$(cat <<'EOF'
chore: scaffold vanity package, tests, pyproject

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: vanity/encoding.py — XRPL base58 + family seed + address encoders

**Files:**
- Create: `xrp_vanity_gpu/vanity/encoding.py`
- Create: `xrp_vanity_gpu/tests/test_encoding.py`

- [ ] **Step 1: Write the failing tests**

`xrp_vanity_gpu/tests/test_encoding.py`:
```python
import os
import secrets

from vanity import encoding


# Known XRPL test vectors from xrpl-py:
# Seed bytes -> family seed string -> account_id (20B) -> address.
#
# These were generated with:
#   from xrpl.core.keypairs import derive_keypair, derive_classic_address
#   from xrpl.core.addresscodec import encode_seed
#   seed_bytes = b"\x00" * 16
#   s = encode_seed(seed_bytes, "ed25519")
#   pub, _ = derive_keypair(s)
#   addr = derive_classic_address(pub)
#
# All-zero seed:
ZERO_SEED_16 = bytes(16)
ZERO_SEED_S = "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r"

# All-0xFF seed:
FF_SEED_16 = b"\xff" * 16
FF_SEED_S = "sEdVD2vhDBKv4F2qrAauJpADAEgHBgL"


def test_b58encode_empty():
    assert encoding.b58encode(b"") == ""


def test_b58encode_single_zero():
    # XRPL alphabet's first char ('r') represents zero.
    assert encoding.b58encode(b"\x00") == "r"


def test_b58encode_known_value():
    # 0x01 -> "p" (second char of alphabet)
    assert encoding.b58encode(b"\x01") == "p"


def test_family_seed_encode_zero():
    assert encoding.family_seed_encode(ZERO_SEED_16) == ZERO_SEED_S


def test_family_seed_encode_ff():
    assert encoding.family_seed_encode(FF_SEED_16) == FF_SEED_S


def test_family_seed_encode_roundtrip_via_xrplpy():
    from xrpl.core.addresscodec import encode_seed
    seed = secrets.token_bytes(16)
    assert encoding.family_seed_encode(seed) == encode_seed(seed, "ed25519")


def test_address_encode_matches_xrplpy():
    from xrpl.core.addresscodec import encode_classic_address
    account_id = secrets.token_bytes(20)
    assert encoding.address_encode(account_id) == encode_classic_address(account_id)
```

- [ ] **Step 2: Run tests, see them fail**

```
source ~/miniconda3/etc/profile.d/conda.sh && conda run -n rapids-23.12 \
  pytest xrp_vanity_gpu/tests/test_encoding.py -v
```
Expected: every test fails with `ModuleNotFoundError: vanity` or `AttributeError`.

- [ ] **Step 3: Implement vanity/encoding.py**

`xrp_vanity_gpu/vanity/encoding.py`:
```python
"""XRPL base58 + family seed + address encoders.

The XRPL base58 alphabet is identical to Bitcoin's alphabet permuted.
See https://xrpl.org/base58-encodings.html.
"""
import hashlib

XRPL_ALPHABET = b"rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
assert len(XRPL_ALPHABET) == 58

# Version prefixes (xrpl.core.addresscodec)
_ED25519_SEED_PREFIX = bytes([0x01, 0xE1, 0x4B])  # "sEd..."
_ACCOUNT_ID_PREFIX = bytes([0x00])  # "r..."


def b58encode(data: bytes) -> str:
    """Encode raw bytes to XRPL-alphabet base58 (no checksum)."""
    # Count leading zero bytes -> leading 'r' chars
    n_leading_zeros = 0
    for b in data:
        if b == 0:
            n_leading_zeros += 1
        else:
            break

    # Convert big-endian bytes to integer, then to base58 digits
    n = int.from_bytes(data, "big")
    digits = []
    while n > 0:
        n, r = divmod(n, 58)
        digits.append(XRPL_ALPHABET[r])
    digits.reverse()

    return (chr(XRPL_ALPHABET[0]) * n_leading_zeros) + bytes(digits).decode("ascii")


def _double_sha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def family_seed_encode(seed16: bytes) -> str:
    """Encode a 16-byte Ed25519 family seed as 'sEd...'."""
    if len(seed16) != 16:
        raise ValueError(f"seed must be 16 bytes, got {len(seed16)}")
    payload = _ED25519_SEED_PREFIX + seed16
    checksum = _double_sha256(payload)[:4]
    return b58encode(payload + checksum)


def address_encode(account_id20: bytes) -> str:
    """Encode a 20-byte account_id (RIPEMD160(SHA256(pubkey))) as 'r...'."""
    if len(account_id20) != 20:
        raise ValueError(f"account_id must be 20 bytes, got {len(account_id20)}")
    payload = _ACCOUNT_ID_PREFIX + account_id20
    checksum = _double_sha256(payload)[:4]
    return b58encode(payload + checksum)
```

- [ ] **Step 4: Run tests, see them pass**

```
source ~/miniconda3/etc/profile.d/conda.sh && conda run -n rapids-23.12 \
  pytest xrp_vanity_gpu/tests/test_encoding.py -v
```
Expected: 7 passed.

If `test_family_seed_encode_zero` fails because the expected `sEdS...` string is wrong (xrpl-py changed conventions), regenerate the constants by running:
```
conda run -n rapids-23.12 python -c "
from xrpl.core.addresscodec import encode_seed
print(encode_seed(b'\\x00'*16, 'ed25519'))
print(encode_seed(b'\\xff'*16, 'ed25519'))
"
```
and update `ZERO_SEED_S` / `FF_SEED_S` in the test file.

- [ ] **Step 5: Commit**

```
git -C /home/hamsa add \
  xrp_vanity_gpu/vanity/encoding.py \
  xrp_vanity_gpu/tests/test_encoding.py
git -C /home/hamsa commit -m "$(cat <<'EOF'
feat: add XRPL base58, family seed, and address encoders

Validated against xrpl-py for both known fixed vectors and random
roundtrip. Pure stdlib (hashlib) — no GPU dependency.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: vanity/sieve.py — CPU pubkey → address + prefix match

**Files:**
- Create: `xrp_vanity_gpu/vanity/sieve.py`
- Create: `xrp_vanity_gpu/tests/test_sieve.py`

- [ ] **Step 1: Write the failing tests**

`xrp_vanity_gpu/tests/test_sieve.py`:
```python
import secrets
from typing import NamedTuple

import pytest

from vanity import encoding, sieve


def _xrplpy_address_from_seed(seed16: bytes) -> tuple[str, str]:
    """Oracle: returns (33B pubkey hex, classic address) for a seed."""
    from xrpl.core.keypairs import derive_keypair, derive_classic_address
    s = encoding.family_seed_encode(seed16)
    pub_hex, _ = derive_keypair(s)
    addr = derive_classic_address(pub_hex)
    return pub_hex, addr


def test_address_from_pubkey_matches_xrplpy():
    seed = secrets.token_bytes(16)
    pub_hex, expected_addr = _xrplpy_address_from_seed(seed)
    pub_bytes = bytes.fromhex(pub_hex)
    assert sieve.address_from_pubkey(pub_bytes) == expected_addr


def test_match_prefix_case_insensitive_hits():
    addr = "rDaimyoFooBar"
    assert sieve.match(addr, "daimyo", case_sensitive=False) is True
    assert sieve.match(addr, "DAIMYO", case_sensitive=False) is True
    assert sieve.match(addr, "Daimyo", case_sensitive=False) is True


def test_match_prefix_case_insensitive_misses():
    addr = "rXyzDaimyo"
    # "Daimyo" is in the address but not at the prefix position.
    assert sieve.match(addr, "daimyo", case_sensitive=False) is False


def test_match_prefix_case_sensitive():
    addr = "rDaimyoFooBar"
    assert sieve.match(addr, "Daimyo", case_sensitive=True) is True
    assert sieve.match(addr, "daimyo", case_sensitive=True) is False


def test_sieve_batch_finds_known_hit():
    # Craft a batch where one pubkey produces an 'rD...' address.
    # We don't know which one apriori, so we generate seeds until xrpl-py
    # gives us an address starting with 'rD', then assemble a 2-pubkey batch
    # where exactly one matches PATTERN='D'.
    target_seed = None
    target_pub = None
    target_addr = None
    for _ in range(200):
        seed = secrets.token_bytes(16)
        pub_hex, addr = _xrplpy_address_from_seed(seed)
        if addr[1] == "D":
            target_seed, target_pub, target_addr = seed, bytes.fromhex(pub_hex), addr
            break
    assert target_seed is not None, "Could not find an 'rD...' address in 200 tries"

    # A second random pubkey that almost certainly does NOT start with 'rD'.
    other_seed = secrets.token_bytes(16)
    other_pub_hex, other_addr = _xrplpy_address_from_seed(other_seed)
    other_pub = bytes.fromhex(other_pub_hex)
    # If we got unlucky, regenerate.
    while other_addr[1] == "D":
        other_seed = secrets.token_bytes(16)
        other_pub_hex, other_addr = _xrplpy_address_from_seed(other_seed)
        other_pub = bytes.fromhex(other_pub_hex)

    pubkeys = target_pub + other_pub
    seeds = target_seed + other_seed
    hits = sieve.sieve_batch(
        pubkeys=pubkeys,
        seeds=seeds,
        pattern="D",
        case_sensitive=True,
        first_attempt_index=0,
    )
    assert len(hits) == 1
    assert hits[0].address == target_addr
    assert hits[0].seed_b58 == encoding.family_seed_encode(target_seed)
```

- [ ] **Step 2: Run tests, see them fail**

```
source ~/miniconda3/etc/profile.d/conda.sh && conda run -n rapids-23.12 \
  pytest xrp_vanity_gpu/tests/test_sieve.py -v
```
Expected: import errors / attribute errors.

- [ ] **Step 3: Implement vanity/sieve.py**

`xrp_vanity_gpu/vanity/sieve.py`:
```python
"""CPU sieve: pubkey -> address -> prefix match."""
import hashlib
from typing import NamedTuple

from Crypto.Hash import RIPEMD160

from vanity import encoding

PUBKEY_LEN = 33  # 0xED prefix + 32-byte compressed Ed25519 point
SEED_LEN = 16


class Match(NamedTuple):
    seed_b58: str
    address: str
    attempt: int


def _ripemd160(data: bytes) -> bytes:
    h = RIPEMD160.new()
    h.update(data)
    return h.digest()


def address_from_pubkey(pubkey33: bytes) -> str:
    """Compute the XRPL classic address from a 33-byte Ed25519 pubkey."""
    if len(pubkey33) != PUBKEY_LEN:
        raise ValueError(f"pubkey must be 33 bytes, got {len(pubkey33)}")
    sha = hashlib.sha256(pubkey33).digest()
    account_id = _ripemd160(sha)
    return encoding.address_encode(account_id)


def match(address: str, pattern: str, case_sensitive: bool) -> bool:
    """Check whether `address[1:1+len(pattern)]` equals `pattern`."""
    if len(address) < 1 + len(pattern):
        return False
    region = address[1 : 1 + len(pattern)]
    if case_sensitive:
        return region == pattern
    return region.lower() == pattern.lower()


def sieve_batch(
    pubkeys: bytes,
    seeds: bytes,
    pattern: str,
    case_sensitive: bool,
    first_attempt_index: int,
) -> list[Match]:
    """Sweep a batch of (seed, pubkey) pairs and return all matches.

    `pubkeys` is B * 33 bytes; `seeds` is B * 16 bytes; both share batch size B.
    `first_attempt_index` is the global attempt number of the first pair, used
    only to populate Match.attempt.
    """
    if len(pubkeys) % PUBKEY_LEN != 0:
        raise ValueError("pubkeys length must be a multiple of 33")
    if len(seeds) % SEED_LEN != 0:
        raise ValueError("seeds length must be a multiple of 16")
    b_from_pubkeys = len(pubkeys) // PUBKEY_LEN
    b_from_seeds = len(seeds) // SEED_LEN
    if b_from_pubkeys != b_from_seeds:
        raise ValueError(
            f"pubkey batch size {b_from_pubkeys} != seed batch size {b_from_seeds}"
        )

    hits: list[Match] = []
    needle = pattern if case_sensitive else pattern.lower()
    n = len(pattern)

    for i in range(b_from_pubkeys):
        pub = pubkeys[i * PUBKEY_LEN : (i + 1) * PUBKEY_LEN]
        addr = address_from_pubkey(pub)
        region = addr[1 : 1 + n]
        if not case_sensitive:
            region = region.lower()
        if region == needle:
            seed = seeds[i * SEED_LEN : (i + 1) * SEED_LEN]
            hits.append(
                Match(
                    seed_b58=encoding.family_seed_encode(seed),
                    address=addr,
                    attempt=first_attempt_index + i,
                )
            )
    return hits
```

- [ ] **Step 4: Run tests, see them pass**

```
source ~/miniconda3/etc/profile.d/conda.sh && conda run -n rapids-23.12 \
  pytest xrp_vanity_gpu/tests/test_sieve.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```
git -C /home/hamsa add \
  xrp_vanity_gpu/vanity/sieve.py \
  xrp_vanity_gpu/tests/test_sieve.py
git -C /home/hamsa commit -m "$(cat <<'EOF'
feat: add CPU sieve (pubkey -> address -> prefix match)

Uses hashlib for SHA-256 and pycryptodome for RIPEMD-160. Validated
against xrpl-py for random pubkeys and a hand-found 'rD...' fixture.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Vendor ed25519-donna at a pinned commit

**Files:**
- Create: `xrp_vanity_gpu/third_party/ed25519-donna/` (vendored source)
- Create: `xrp_vanity_gpu/third_party/README.md`

- [ ] **Step 1: Clone donna into a temp dir and pin a commit**

```
TMPDIR=$(mktemp -d)
git clone https://github.com/floodyberry/ed25519-donna "$TMPDIR/donna"
git -C "$TMPDIR/donna" rev-parse HEAD     # record this hash as PINNED_COMMIT
git -C "$TMPDIR/donna" log -1 --format="%H %ci"
```

Record the commit hash. The default-branch HEAD is fine; pin it for reproducibility.

- [ ] **Step 2: Copy donna source into third_party/ (no .git, no build artifacts)**

```
mkdir -p xrp_vanity_gpu/third_party/ed25519-donna
cp -r "$TMPDIR/donna/"*.h "$TMPDIR/donna/"*.c xrp_vanity_gpu/third_party/ed25519-donna/ 2>/dev/null || true
# Inspect what got copied:
ls xrp_vanity_gpu/third_party/ed25519-donna/
```

The relevant files for our port are: `ed25519.h`, `ed25519-donna.h`, `curve25519-donna-64bit.h`, `ed25519-donna-basepoint-table.h`, `modm-donna-64bit.h`. Copy the entire flat source set; donna is small (~30 files, all < 1500 LOC each).

- [ ] **Step 3: Write third_party/README.md**

```markdown
# third_party

## ed25519-donna

Source: https://github.com/floodyberry/ed25519-donna
License: Public domain (CC0). See `ed25519-donna/LICENSE` (if present)
or the file headers.

Pinned commit: <PASTE PINNED_COMMIT here>

We use the 64-bit code paths:
- `curve25519-donna-64bit.h` for field arithmetic.
- `ed25519-donna.h` for group ops and `ge_scalarmult_base`.
- `ed25519-donna-basepoint-table.h` for the precomputed base table.

These are referenced (not modified) for the CUDA port in
`kernels/ed25519_kernel.cu`. The donna C source is also built as a
shared library by `tools/Makefile` and used to generate test vectors
in `tests/data/ed25519_vectors.json`.
```

- [ ] **Step 4: Commit**

```
git -C /home/hamsa add xrp_vanity_gpu/third_party/
git -C /home/hamsa commit -m "$(cat <<'EOF'
chore: vendor ed25519-donna at <PINNED_COMMIT>

Source dropped into third_party/ for the CUDA port and for building
reference test vectors. License is public domain (CC0).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Build donna reference + generate Ed25519 test vectors

**Files:**
- Create: `xrp_vanity_gpu/tools/Makefile`
- Create: `xrp_vanity_gpu/tools/dump_ed25519_vectors.c`
- Create: `xrp_vanity_gpu/tools/dump_ed25519_vectors.py`
- Create: `xrp_vanity_gpu/tests/data/ed25519_vectors.json` (generated, committed)

- [ ] **Step 1: Write tools/dump_ed25519_vectors.c**

Reads 32-byte scalars from stdin (one at a time), runs
`ge_scalarmult_base`, writes the 32-byte compressed point to stdout.

```c
/* Reads 32-byte little-endian scalars from stdin; writes 32-byte
   compressed Edwards points to stdout. One pair per invocation cycle. */
#include <stdio.h>
#include <string.h>
#include "ed25519-donna.h"

int main(void) {
    unsigned char sk[32];
    unsigned char pk[32];
    bignum256modm a;
    ge25519 ALIGN(16) A;

    while (fread(sk, 1, 32, stdin) == 32) {
        expand256_modm(a, sk, 32);
        ge25519_scalarmult_base_niels(&A, ge25519_niels_base_multiples, a);
        ge25519_pack(pk, &A);
        if (fwrite(pk, 1, 32, stdout) != 32) return 1;
        fflush(stdout);
    }
    return 0;
}
```

Note: the exact function names depend on the donna API version we vendored. If `ge25519_scalarmult_base_niels` is not the public symbol, use `ge25519_scalarmult_base` (no `_niels`) and skip the `ge25519_niels_base_multiples` argument. Inspect `third_party/ed25519-donna/ed25519-donna.h` to confirm.

- [ ] **Step 2: Write tools/Makefile**

```make
# Build donna as a static archive, link the vector dumper against it.
DONNA_DIR := ../third_party/ed25519-donna
CFLAGS := -O3 -DED25519_REFHASH -DED25519_CUSTOMRANDOM -I$(DONNA_DIR)
# ED25519_REFHASH uses donna's bundled SHA-512 so we don't need libssl.
# ED25519_CUSTOMRANDOM avoids /dev/urandom dependency (we don't sign).

all: dump_ed25519_vectors

ed25519.o: $(DONNA_DIR)/ed25519.c
	$(CC) $(CFLAGS) -c $< -o $@

dump_ed25519_vectors: dump_ed25519_vectors.c ed25519.o
	$(CC) $(CFLAGS) $^ -o $@

clean:
	rm -f *.o dump_ed25519_vectors
```

If donna requires extra `-D` flags (e.g., `-DED25519_64BIT`), check the donna README in `third_party/ed25519-donna/` and add them.

- [ ] **Step 3: Build the dumper**

```
cd xrp_vanity_gpu/tools && make
ls -la dump_ed25519_vectors
```

If the build fails, fix `Makefile` flags and re-check donna's `README` for required `-D` defines.

- [ ] **Step 4: Write tools/dump_ed25519_vectors.py — driver that calls the C program**

```python
"""Driver: generates 1000 random scalars, pipes them through donna,
captures compressed points, writes JSON to tests/data/ed25519_vectors.json.

The scalar used by XRPL is `SHA-512(seed)[:32]`, but for the kernel-port
unit test we generate scalars directly: 32 random bytes, no clamp. Donna's
`expand256_modm` reduces mod L internally, so any 32-byte value is fine.
"""
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BINARY = REPO / "tools" / "dump_ed25519_vectors"
OUT = REPO / "tests" / "data" / "ed25519_vectors.json"

N_VECTORS = 1000


def main() -> None:
    if not BINARY.exists():
        sys.exit(f"build first: cd {BINARY.parent} && make")

    scalars = [secrets.token_bytes(32) for _ in range(N_VECTORS)]
    stdin = b"".join(scalars)
    proc = subprocess.run(
        [str(BINARY)], input=stdin, capture_output=True, check=True
    )
    if len(proc.stdout) != 32 * N_VECTORS:
        sys.exit(
            f"expected {32 * N_VECTORS} stdout bytes, got {len(proc.stdout)}"
        )

    vectors = [
        {
            "scalar_hex": scalars[i].hex(),
            "point_hex": proc.stdout[i * 32 : (i + 1) * 32].hex(),
        }
        for i in range(N_VECTORS)
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, indent=2))
    print(f"wrote {N_VECTORS} vectors to {OUT}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Generate vectors**

```
conda run -n rapids-23.12 python xrp_vanity_gpu/tools/dump_ed25519_vectors.py
ls -la xrp_vanity_gpu/tests/data/ed25519_vectors.json
head -c 400 xrp_vanity_gpu/tests/data/ed25519_vectors.json
```
Expected: file is ~120 KB, contains 1000 entries.

- [ ] **Step 6: Add a sanity check: vectors round-trip against xrpl-py for the SHA-512 path**

Append to `xrp_vanity_gpu/tests/test_sieve.py`:
```python
def test_vector_file_loads():
    import json, pathlib
    p = pathlib.Path(__file__).parent / "data" / "ed25519_vectors.json"
    vecs = json.loads(p.read_text())
    assert len(vecs) == 1000
    assert len(bytes.fromhex(vecs[0]["scalar_hex"])) == 32
    assert len(bytes.fromhex(vecs[0]["point_hex"])) == 32
```

Run:
```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_sieve.py::test_vector_file_loads -v
```
Expected: PASS.

- [ ] **Step 7: Commit**

```
git -C /home/hamsa add \
  xrp_vanity_gpu/tools/Makefile \
  xrp_vanity_gpu/tools/dump_ed25519_vectors.c \
  xrp_vanity_gpu/tools/dump_ed25519_vectors.py \
  xrp_vanity_gpu/tests/data/ed25519_vectors.json \
  xrp_vanity_gpu/tests/test_sieve.py

# Add a .gitignore for build artifacts
cat > xrp_vanity_gpu/tools/.gitignore <<'EOF'
*.o
dump_ed25519_vectors
EOF
git -C /home/hamsa add xrp_vanity_gpu/tools/.gitignore

git -C /home/hamsa commit -m "$(cat <<'EOF'
feat: generate ed25519 reference vectors via donna

1000 (scalar, compressed_point) pairs from ed25519-donna's
ge_scalarmult_base. These are the oracle for the CUDA port.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: vanity/gpu.py skeleton — kernel source loader and compile

**Files:**
- Create: `xrp_vanity_gpu/vanity/gpu.py`
- Create: `xrp_vanity_gpu/tests/test_gpu_compile.py`
- Create: `xrp_vanity_gpu/kernels/ed25519_kernel.cu` (stub — empty preamble + placeholder)
- Create: `xrp_vanity_gpu/kernels/pipeline_kernel.cu` (stub)

Establish the CuPy kernel-loading machinery early so we can iterate on Ed25519 with fast feedback.

- [ ] **Step 1: Write the failing test**

`xrp_vanity_gpu/tests/test_gpu_compile.py`:
```python
import pytest


@pytest.mark.gpu
def test_module_compiles():
    """The concatenated kernel source must compile under NVRTC."""
    from vanity import gpu
    mod = gpu.compile_module()
    # Smoke: the SHA-512 kernel symbol must be exported by name.
    # (The exact name comes from existing sha_kernels.cu.)
    # Just verify we got a RawModule and didn't throw.
    assert mod is not None
```

- [ ] **Step 2: Run, see it fail (module not present)**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_gpu_compile.py -v
```
Expected: `ModuleNotFoundError: vanity.gpu`.

- [ ] **Step 3: Write stub ed25519_kernel.cu (empty body — we fill it later)**

`xrp_vanity_gpu/kernels/ed25519_kernel.cu`:
```c
/* Ed25519 kernel — donna port. Populated incrementally in Task 7-8.

   Conventions:
   - All device functions take/return raw uint64_t arrays for field elements.
   - Field element representation: 5 x 51-bit limbs.
   - Group element: extended Edwards coordinates (ge_p3 equivalent).
   - Multiply uses __umul64hi for the high 64 bits of a 64x64 product. */

/* Placeholder: until field arithmetic is ported, we expose nothing. */
```

- [ ] **Step 4: Write stub pipeline_kernel.cu**

`xrp_vanity_gpu/kernels/pipeline_kernel.cu`:
```c
/* pipeline kernel: seed (16B) -> sha512[:32] -> ge_scalarmult_base ->
   point_compress -> 33B pubkey (0xED || compressed point).

   Populated in Task 9 once Ed25519 is in place. For now, a no-op so
   the concatenated source compiles. */

extern "C" __global__
void pipeline_noop(unsigned int n) {
    /* Intentionally empty so the module compiles before the real
       pipeline kernel exists. */
    (void)n;
}
```

- [ ] **Step 5: Write vanity/gpu.py**

`xrp_vanity_gpu/vanity/gpu.py`:
```python
"""CuPy kernel-loading and launch wrappers."""
from pathlib import Path

import cupy as cp
import numpy as np

KERNELS_DIR = Path(__file__).resolve().parents[1] / "kernels"

NVRTC_PREAMBLE = """
typedef unsigned char       uint8_t;
typedef unsigned int        uint32_t;
typedef unsigned long long  uint64_t;
typedef long long           int64_t;
"""

# Order matters: dependencies first.
KERNEL_FILES = ["sha_kernels.cu", "ed25519_kernel.cu", "pipeline_kernel.cu"]


def _read_kernel_sources() -> str:
    chunks = [NVRTC_PREAMBLE]
    for name in KERNEL_FILES:
        chunks.append((KERNELS_DIR / name).read_text())
    return "\n".join(chunks)


def compile_module() -> cp.RawModule:
    """Compile the concatenated kernel source under NVRTC."""
    source = _read_kernel_sources()
    try:
        return cp.RawModule(
            code=source,
            backend="nvrtc",
            options=("--std=c++14",),
        )
    except cp.cuda.compiler.CompileException as e:
        # Surface the NVRTC log with line numbers prefixed.
        numbered = "\n".join(
            f"{i+1:4d}: {line}" for i, line in enumerate(source.splitlines())
        )
        raise RuntimeError(
            f"NVRTC compile failed:\n{e}\n\n--- FULL SOURCE ---\n{numbered}"
        ) from e
```

- [ ] **Step 6: Run test, see it pass**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_gpu_compile.py -v
```
Expected: PASS.

If it fails because `sha_kernels.cu` already starts with its own `typedef` declarations (duplicates of NVRTC_PREAMBLE), remove the duplicate typedefs from `sha_kernels.cu` so only `NVRTC_PREAMBLE` declares them. Re-run.

- [ ] **Step 7: Commit**

```
git -C /home/hamsa add \
  xrp_vanity_gpu/vanity/gpu.py \
  xrp_vanity_gpu/kernels/ed25519_kernel.cu \
  xrp_vanity_gpu/kernels/pipeline_kernel.cu \
  xrp_vanity_gpu/tests/test_gpu_compile.py \
  xrp_vanity_gpu/kernels/sha_kernels.cu

git -C /home/hamsa commit -m "$(cat <<'EOF'
feat: add CuPy kernel loader + ed25519/pipeline stubs

Concatenates sha_kernels.cu, ed25519_kernel.cu, pipeline_kernel.cu
behind a shared NVRTC preamble. Stubs compile cleanly so subsequent
tasks can incrementally fill ed25519_kernel.cu with fast feedback.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Port donna field arithmetic (`fe_*`) to CUDA + unit-test

**Files:**
- Modify: `xrp_vanity_gpu/kernels/ed25519_kernel.cu`
- Create: `xrp_vanity_gpu/kernels/ed25519_fe_test.cu` (test launcher)
- Create: `xrp_vanity_gpu/tests/test_ed25519_fe.py`

This task is "mechanical transliteration from `third_party/ed25519-donna/curve25519-donna-64bit.h`". **The donna source is the authoritative reference — the implementer reads each function body from the vendored file rather than from this plan.** What the plan specifies here is (a) the exact list of donna symbols to port, (b) the transliteration rules, (c) the per-function test contract (tests gate correctness; if a test passes the port is correct).

Donna's 64-bit field uses 5 × 51-bit limbs in `uint64_t[5]`. The port is:

1. Add `__device__` to every function.
2. Replace `static inline` with `__device__ __forceinline__`.
3. Replace `__uint128_t` multiplications with `__umul64hi(a, b) << 64 | (a * b)` split into hi/lo `uint64_t`. The donna helper `mul64x64_128` becomes a small inline macro using `__umul64hi`.
4. Keep limb layout and constants byte-identical.

- [ ] **Step 1: Write the failing test (defines the contract for the kernel)**

`xrp_vanity_gpu/tests/test_ed25519_fe.py`:
```python
"""Unit-test the CUDA fe_* (field arithmetic) port against pure-Python
mod-2^255-19 arithmetic. We test fe_mul, fe_sq, fe_add, fe_sub, fe_invert.
"""
import json
import secrets
from pathlib import Path

import cupy as cp
import numpy as np
import pytest

from vanity import gpu

P = (1 << 255) - 19


def _packed_limbs_to_int(limbs5: list[int]) -> int:
    """Convert 5 x 51-bit limbs (little-endian) into one big int mod p."""
    return sum((v & ((1 << 51) - 1)) << (51 * i) for i, v in enumerate(limbs5)) % P


def _int_to_packed_limbs(n: int) -> list[int]:
    n %= P
    mask = (1 << 51) - 1
    return [(n >> (51 * i)) & mask for i in range(5)]


@pytest.mark.gpu
def test_fe_mul_matches_python():
    mod = gpu.compile_module()
    fe_mul_test = mod.get_function("fe_mul_test")

    N = 64
    rng = np.random.default_rng(0xC0FFEE)
    a_ints = [int.from_bytes(rng.bytes(32), "little") % P for _ in range(N)]
    b_ints = [int.from_bytes(rng.bytes(32), "little") % P for _ in range(N)]

    a_packed = np.array(
        [_int_to_packed_limbs(x) for x in a_ints], dtype=np.uint64
    ).reshape(-1)
    b_packed = np.array(
        [_int_to_packed_limbs(x) for x in b_ints], dtype=np.uint64
    ).reshape(-1)

    d_a = cp.asarray(a_packed)
    d_b = cp.asarray(b_packed)
    d_out = cp.zeros(5 * N, dtype=cp.uint64)

    fe_mul_test(
        (N,), (1,),
        (d_a, d_b, d_out, np.uint32(N)),
    )
    out = cp.asnumpy(d_out).reshape(N, 5).tolist()

    for i, (ai, bi) in enumerate(zip(a_ints, b_ints)):
        expected = (ai * bi) % P
        got = _packed_limbs_to_int(out[i])
        assert got == expected, f"row {i}: expected {expected:x}, got {got:x}"
```

Add analogous tests for `fe_sq_test`, `fe_add_test`, `fe_sub_test`,
`fe_invert_test` (same pattern; for `fe_invert`, expected =
`pow(ai, P - 2, P)` and skip rows where `ai == 0`).

- [ ] **Step 2: Run, see it fail (no `fe_mul_test` symbol yet)**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_ed25519_fe.py -v
```
Expected: failure surfaces an NVRTC error or `cannot find function "fe_mul_test"`.

- [ ] **Step 3: Port donna's `curve25519-donna-64bit.h` into ed25519_kernel.cu**

Open `xrp_vanity_gpu/third_party/ed25519-donna/curve25519-donna-64bit.h` side-by-side and transliterate. Required functions (donna names): `curve25519_copy`, `curve25519_add`, `curve25519_add_after_basic`, `curve25519_sub`, `curve25519_sub_after_basic`, `curve25519_neg`, `curve25519_mul`, `curve25519_square`, `curve25519_square_times`, `curve25519_expand`, `curve25519_contract`, `curve25519_recip` (or `curve25519_pow_two252m3`).

For each, replace:
- `static DONNA_INLINE` → `__device__ __forceinline__`
- `uint128_t` (donna's typedef) → split into two `uint64_t` (hi/lo) using `__umul64hi`. Donna provides `mul64x64_128(out, a, b)` macro — port it as:
  ```c
  __device__ __forceinline__ void mul64x64_128(uint64_t *hi, uint64_t *lo, uint64_t a, uint64_t b) {
      *lo = a * b;
      *hi = __umul64hi(a, b);
  }
  ```
  Then replace every `t128_a += (uint128_t)a*b` accumulator with explicit 128-bit addition: maintain `(hi_a, lo_a)`, add `(hi_b, lo_b)` with carry. Use `__umul64hi` for the high half of each `a*b` term.

  Donna's `curve25519_mul` accumulates 10 such terms — write the carry propagation explicitly. This is ~80 lines of mechanical code.

Add test launchers at the bottom of `ed25519_kernel.cu`:
```c
extern "C" __global__
void fe_mul_test(const uint64_t *a, const uint64_t *b, uint64_t *out, uint32_t n) {
    uint32_t i = blockIdx.x;
    if (i >= n) return;
    bignum25519 ra, rb, rc;
    /* bignum25519 is the donna typedef for uint64_t[5]; if not in scope,
       declare it: typedef uint64_t bignum25519[5]; */
    for (int j = 0; j < 5; j++) { ra[j] = a[i*5+j]; rb[j] = b[i*5+j]; }
    curve25519_mul(rc, ra, rb);
    for (int j = 0; j < 5; j++) out[i*5+j] = rc[j];
}
```
And analogous `fe_sq_test`, `fe_add_test`, `fe_sub_test`, `fe_invert_test`.

- [ ] **Step 4: Re-run the tests until all pass**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_ed25519_fe.py -v
```
Expected: all PASS. If `fe_mul_test` passes but `fe_invert_test` fails, the bug is in `curve25519_recip` — re-inspect donna's source for that function specifically and verify the squaring chain length.

**Debug tip:** if `fe_mul_test` produces wrong answers, dump intermediate `(hi, lo)` pairs from the kernel via `printf` (NVRTC supports device printf). Compare against a side-by-side pure-Python multiplier that mimics the 5-limb schedule.

- [ ] **Step 5: Commit**

```
git -C /home/hamsa add \
  xrp_vanity_gpu/kernels/ed25519_kernel.cu \
  xrp_vanity_gpu/tests/test_ed25519_fe.py
git -C /home/hamsa commit -m "$(cat <<'EOF'
feat: port donna fe_* field arithmetic to CUDA

5x51-bit limb representation; 64x64->128 via __umul64hi. Validated
against pure-Python mod-2^255-19 arithmetic for fe_mul, fe_sq,
fe_add, fe_sub, fe_invert across 64 random inputs each.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Port donna group arithmetic + `ge_scalarmult_base` + `ge_pack`

**Files:**
- Modify: `xrp_vanity_gpu/kernels/ed25519_kernel.cu`
- Create: `xrp_vanity_gpu/tests/test_ed25519_vectors.py`

- [ ] **Step 1: Write the failing test**

`xrp_vanity_gpu/tests/test_ed25519_vectors.py`:
```python
"""End-to-end Ed25519 kernel test against donna-generated vectors."""
import json
from pathlib import Path

import cupy as cp
import numpy as np
import pytest

from vanity import gpu

VECTORS = json.loads(
    (Path(__file__).parent / "data" / "ed25519_vectors.json").read_text()
)


@pytest.mark.gpu
@pytest.mark.slow
def test_scalarmult_base_all_1000_vectors():
    mod = gpu.compile_module()
    k = mod.get_function("scalarmult_base_test")

    N = len(VECTORS)
    scalars = np.frombuffer(
        b"".join(bytes.fromhex(v["scalar_hex"]) for v in VECTORS),
        dtype=np.uint8,
    ).reshape(N, 32)
    expected = [bytes.fromhex(v["point_hex"]) for v in VECTORS]

    d_scalars = cp.asarray(scalars).reshape(-1)
    d_out = cp.zeros(N * 32, dtype=cp.uint8)

    threads = 128
    blocks = (N + threads - 1) // threads
    k((blocks,), (threads,), (d_scalars, d_out, np.uint32(N)))

    out = cp.asnumpy(d_out).reshape(N, 32)
    mismatches = []
    for i in range(N):
        if bytes(out[i]) != expected[i]:
            mismatches.append((i, bytes(out[i]).hex(), expected[i].hex()))
            if len(mismatches) > 5:
                break
    assert not mismatches, f"first mismatches: {mismatches}"
```

- [ ] **Step 2: Run, see it fail (no `scalarmult_base_test` symbol)**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_ed25519_vectors.py -v
```

- [ ] **Step 3: Port donna's group code into ed25519_kernel.cu**

Same convention as Task 7: the vendored donna source is the authoritative reference; the plan specifies which symbols to port and the test contract.

From `third_party/ed25519-donna/ed25519-donna.h`, port:

1. The `ge25519` struct (extended Edwards coords: `x, y, z, t`, each a `bignum25519`).
2. The "niels" precomputed point format (`ge25519_niels`).
3. `ge25519_double`, `ge25519_add`, `ge25519_madd` (mixed addition).
4. `ge25519_scalarmult_base_niels` (signed-radix-16 windowed mult against the precomputed base table).
5. `ge25519_pack` (point compression to 32 bytes).
6. `expand256_modm` (decompose 32-byte scalar into signed-radix-16 digits).

For the base-point precomputed table, **embed it as a `__constant__ __device__` array** by including the data from `third_party/ed25519-donna/ed25519-donna-basepoint-table.h`. The table is large (~30 KB) but fits comfortably in constant memory.

Donna's `ed25519-donna-basepoint-table.h` is a static C initialiser; copy the array literal verbatim and wrap it as:
```c
__constant__ ge25519_niels ge25519_niels_base_multiples_d[256] = {
    /* ... donna's literal contents ... */
};
```
(The exact dimensions depend on donna's table layout — confirm by reading the header.)

Add the test launcher kernel:
```c
extern "C" __global__
void scalarmult_base_test(const unsigned char *scalars, unsigned char *out, uint32_t n) {
    uint32_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= n) return;

    bignum256modm a;
    ge25519 A;
    expand256_modm(a, scalars + i * 32, 32);
    ge25519_scalarmult_base_niels(&A, ge25519_niels_base_multiples_d, a);
    ge25519_pack(out + i * 32, &A);
}
```

NVRTC notes:
- All function pointers / call graphs must be statically resolvable. No virtual dispatch, no host-only calls.
- The `ALIGN(16)` macro donna uses is not needed under NVRTC; replace with `__align__(16)` or just drop it.
- If `__umul64hi` is undefined, add `#include <cuda_runtime.h>` — but NVRTC does NOT support `#include`. Instead, declare it explicitly:
  ```c
  extern "C" __device__ unsigned long long __umul64hi(unsigned long long, unsigned long long);
  ```
  (Older NVRTC versions need this; CUDA 12.2 may auto-resolve it.)

- [ ] **Step 4: Re-run the test, iterate to green**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_ed25519_vectors.py -v
```

Expected after correct port: 1000 vectors all match.

If the first byte is right but later bytes diverge, the bug is almost certainly in the table layout or in `ge_pack` (which encodes Y || sign-bit-of-X). If random vectors diverge, the bug is in scalar decomposition (`expand256_modm`) or in `ge_madd`.

**Triage helper:** add a `scalarmult_zero_test` kernel that runs `ge_scalarmult_base_niels(&A, table, 0)` and packs — it should output `0x01, 0x00, ..., 0x00` (the identity element). If THAT fails, the table embedding is wrong.

- [ ] **Step 5: Commit**

```
git -C /home/hamsa add \
  xrp_vanity_gpu/kernels/ed25519_kernel.cu \
  xrp_vanity_gpu/tests/test_ed25519_vectors.py
git -C /home/hamsa commit -m "$(cat <<'EOF'
feat: port donna group ops + scalarmult_base to CUDA

ge25519 extended Edwards coords, mixed addition, niels precomputed
base table in __constant__ memory, signed-radix-16 windowed scalar
mult, ge_pack compression. All 1000 donna reference vectors match.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Build `pipeline_kernel` (seed → pubkey end-to-end)

**Files:**
- Modify: `xrp_vanity_gpu/kernels/pipeline_kernel.cu`
- Create: `xrp_vanity_gpu/tests/test_pipeline_kernel.py`

- [ ] **Step 1: Write the failing test**

`xrp_vanity_gpu/tests/test_pipeline_kernel.py`:
```python
"""End-to-end pipeline kernel test against xrpl-py.

For 1000 random 16-byte seeds, the pipeline kernel's 33-byte output
must equal the public-key bytes from xrpl-py's derive_keypair.
"""
import secrets

import cupy as cp
import numpy as np
import pytest

from vanity import encoding, gpu


def _xrplpy_pubkey(seed16: bytes) -> bytes:
    from xrpl.core.keypairs import derive_keypair
    s = encoding.family_seed_encode(seed16)
    pub_hex, _ = derive_keypair(s)
    return bytes.fromhex(pub_hex)


@pytest.mark.gpu
@pytest.mark.slow
def test_pipeline_kernel_1000_seeds():
    mod = gpu.compile_module()
    k = mod.get_function("pipeline")

    N = 1000
    seeds = b"".join(secrets.token_bytes(16) for _ in range(N))
    expected = [_xrplpy_pubkey(seeds[i * 16 : (i + 1) * 16]) for i in range(N)]

    d_seeds = cp.asarray(np.frombuffer(seeds, dtype=np.uint8))
    d_out = cp.zeros(N * 33, dtype=cp.uint8)

    threads = 128
    blocks = (N + threads - 1) // threads
    k((blocks,), (threads,), (d_seeds, d_out, np.uint32(N)))

    out = cp.asnumpy(d_out)
    mismatches = []
    for i in range(N):
        got = bytes(out[i * 33 : (i + 1) * 33])
        if got != expected[i]:
            mismatches.append((i, got.hex(), expected[i].hex()))
            if len(mismatches) > 5:
                break
    assert not mismatches, f"first mismatches: {mismatches}"
```

- [ ] **Step 2: Run, see it fail**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_pipeline_kernel.py -v
```
Expected: failure — `pipeline` exists but is the no-op stub.

- [ ] **Step 3: Implement pipeline kernel**

Replace `xrp_vanity_gpu/kernels/pipeline_kernel.cu` with:

```c
/* pipeline: seed (16B) -> sha512[:32] -> scalar_mult_base -> 33B pubkey.
   Depends on SHA-512 device functions from sha_kernels.cu and Ed25519
   functions from ed25519_kernel.cu (which are all in the same translation
   unit after Python-level concatenation in vanity/gpu.py). */

/* SHA-512 device entry point: assumed exported by sha_kernels.cu as
   __device__ void sha512_device(const uint8_t *in, uint32_t in_len, uint8_t out[64]);
   If the existing kernel exposes a different signature (e.g. takes message
   schedule pointers), adapt this wrapper or expose a shim there. */

extern "C" __global__
void pipeline(
    const unsigned char * __restrict__ seeds,    /* B * 16 */
    unsigned char * __restrict__ pubkeys,        /* B * 33 */
    unsigned int B
) {
    unsigned int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= B) return;

    uint8_t hash[64];
    sha512_device(seeds + i * 16, 16, hash);

    /* First 32 bytes of SHA-512 are the Ed25519 scalar; no clamping
       (XRPL convention). */
    bignum256modm a;
    ge25519 A;
    expand256_modm(a, hash, 32);
    ge25519_scalarmult_base_niels(&A, ge25519_niels_base_multiples_d, a);

    pubkeys[i * 33] = 0xED;
    ge25519_pack(pubkeys + i * 33 + 1, &A);
}
```

If `sha_kernels.cu` does NOT expose a clean `sha512_device(in, len, out)` device wrapper, ADD one near the bottom of `sha_kernels.cu` that wraps the existing implementation. Example:
```c
__device__ void sha512_device(const uint8_t *in, uint32_t in_len, uint8_t out[64]) {
    /* Inline call to the existing SHA-512 routines.
       Pad in_len bytes (always <= 128 since we feed 16-byte seeds),
       compute one or two blocks, write 64-byte big-endian digest. */
    /* ... use existing helpers ... */
}
```
(Look at the existing kernel's structure first; do not duplicate work.)

- [ ] **Step 4: Re-run the test, iterate to green**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_pipeline_kernel.py -v
```
Expected: PASS (all 1000 match).

If only the first byte (0xED) matches and the next 32 diverge, the bug is in SHA-512 padding for 16-byte input. Spot-check with one seed:
```
conda run -n rapids-23.12 python -c "
import hashlib
print(hashlib.sha512(b'\\x00'*16).hexdigest())
"
```
Compare against the GPU's `hash[0..32]` for the zero seed (add a debug-only test that dumps SHA-512 output).

- [ ] **Step 5: Commit**

```
git -C /home/hamsa add \
  xrp_vanity_gpu/kernels/pipeline_kernel.cu \
  xrp_vanity_gpu/kernels/sha_kernels.cu \
  xrp_vanity_gpu/tests/test_pipeline_kernel.py
git -C /home/hamsa commit -m "$(cat <<'EOF'
feat: pipeline kernel (seed -> sha512[:32] -> scalarmult -> 33B pubkey)

End-to-end CUDA path matches xrpl-py public keys for 1000 random
16-byte seeds. SHA-256 + RIPEMD-160 + base58check remain on CPU per
the hybrid design.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: vanity/gpu.py — `VanityGpu` class with batch runner

**Files:**
- Modify: `xrp_vanity_gpu/vanity/gpu.py`
- Create: `xrp_vanity_gpu/tests/test_gpu_batch.py`

- [ ] **Step 1: Write the failing test**

`xrp_vanity_gpu/tests/test_gpu_batch.py`:
```python
import secrets
import pytest

from vanity import encoding, gpu, sieve


def _xrplpy_pubkey(seed16: bytes) -> bytes:
    from xrpl.core.keypairs import derive_keypair
    s = encoding.family_seed_encode(seed16)
    pub_hex, _ = derive_keypair(s)
    return bytes.fromhex(pub_hex)


@pytest.mark.gpu
def test_vanity_gpu_run_batch():
    B = 256
    g = gpu.VanityGpu(batch_size=B)
    seeds = b"".join(secrets.token_bytes(16) for _ in range(B))
    pubkeys = g.run_batch(seeds)
    assert len(pubkeys) == B * 33
    # Spot-check 5 random rows against xrpl-py.
    import random
    for idx in random.sample(range(B), 5):
        expected = _xrplpy_pubkey(seeds[idx * 16 : (idx + 1) * 16])
        got = pubkeys[idx * 33 : (idx + 1) * 33]
        assert got == expected, f"row {idx} mismatch"
```

- [ ] **Step 2: Run, fail (no `VanityGpu`)**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_gpu_batch.py -v
```

- [ ] **Step 3: Add `VanityGpu` to vanity/gpu.py**

Append to `xrp_vanity_gpu/vanity/gpu.py`:
```python
class VanityGpu:
    """Owns the compiled module and per-batch device/host buffers."""

    def __init__(self, batch_size: int):
        self.batch_size = batch_size
        self.module = compile_module()
        self._pipeline = self.module.get_function("pipeline")

        self._d_seeds = cp.zeros(batch_size * 16, dtype=cp.uint8)
        self._d_pubkeys = cp.zeros(batch_size * 33, dtype=cp.uint8)
        self._h_pubkeys = cp.cuda.alloc_pinned_memory(batch_size * 33)

        self._threads = 256
        self._blocks = (batch_size + self._threads - 1) // self._threads

    def run_batch(self, seeds: bytes) -> bytes:
        if len(seeds) != self.batch_size * 16:
            raise ValueError(
                f"seeds must be {self.batch_size * 16} bytes, got {len(seeds)}"
            )
        h_seeds = np.frombuffer(seeds, dtype=np.uint8)
        self._d_seeds.set(h_seeds)
        self._pipeline(
            (self._blocks,), (self._threads,),
            (self._d_seeds, self._d_pubkeys, np.uint32(self.batch_size)),
        )
        cp.cuda.runtime.memcpy(
            int(self._h_pubkeys.ptr),
            int(self._d_pubkeys.data.ptr),
            self.batch_size * 33,
            cp.cuda.runtime.memcpyDeviceToHost,
        )
        return bytes(memoryview(self._h_pubkeys)[: self.batch_size * 33])
```

- [ ] **Step 4: Run test, see it pass**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_gpu_batch.py -v
```

If the pinned memory API differs in CuPy 13.4.1, fall back to a plain `bytes(cp.asnumpy(self._d_pubkeys))` — slightly slower but correct.

- [ ] **Step 5: Commit**

```
git -C /home/hamsa add xrp_vanity_gpu/vanity/gpu.py xrp_vanity_gpu/tests/test_gpu_batch.py
git -C /home/hamsa commit -m "$(cat <<'EOF'
feat: VanityGpu — batch-runner with pinned host buffer

Allocates device seed/pubkey buffers once at construction; run_batch
launches the pipeline kernel and copies the 33B-per-candidate
pubkeys back to a pinned host buffer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: vanity/stats.py — throughput counter

**Files:**
- Create: `xrp_vanity_gpu/vanity/stats.py`
- Create: `xrp_vanity_gpu/tests/test_stats.py`

- [ ] **Step 1: Write the failing test**

`xrp_vanity_gpu/tests/test_stats.py`:
```python
import time

from vanity.stats import StatsPrinter


def test_stats_records_and_formats():
    sp = StatsPrinter(interval_sec=0.05)
    sp.tick(processed=1_000_000)
    sp.tick(processed=1_000_000)
    time.sleep(0.06)
    line = sp.tick(processed=1_000_000, force_emit=True)
    assert line is not None
    assert "M/s" in line
    assert "matches" in line
    assert "elapsed" in line


def test_stats_skip_within_interval():
    sp = StatsPrinter(interval_sec=10.0)
    sp.tick(processed=1_000_000)
    line = sp.tick(processed=1_000_000)
    assert line is None  # too soon to emit
```

- [ ] **Step 2: Run, fail**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_stats.py -v
```

- [ ] **Step 3: Implement vanity/stats.py**

```python
"""Throughput counter for the vanity search loop."""
import datetime as _dt
import time


def _fmt_count(n: float) -> str:
    if n >= 1e9:
        return f"{n/1e9:.1f}G"
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.1f}K"
    return f"{n:.0f}"


class StatsPrinter:
    def __init__(self, interval_sec: float):
        self.interval = interval_sec
        self.start = time.monotonic()
        self.last_emit = self.start
        self.last_total = 0
        self.total = 0
        self.matches = 0

    def add_match(self) -> None:
        self.matches += 1

    def tick(self, processed: int, force_emit: bool = False) -> str | None:
        self.total += processed
        now = time.monotonic()
        if not force_emit and (now - self.last_emit) < self.interval:
            return None

        window = now - self.last_emit
        window_count = self.total - self.last_total
        inst = window_count / window if window > 0 else 0.0
        elapsed = now - self.start
        avg = self.total / elapsed if elapsed > 0 else 0.0

        self.last_emit = now
        self.last_total = self.total

        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"[{ts}] {_fmt_count(inst)}/s  avg {_fmt_count(avg)}/s  "
            f"total {_fmt_count(self.total)}  "
            f"matches {self.matches}  elapsed {int(elapsed)}s"
        )
```

- [ ] **Step 4: Run, pass**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_stats.py -v
```

- [ ] **Step 5: Commit**

```
git -C /home/hamsa add xrp_vanity_gpu/vanity/stats.py xrp_vanity_gpu/tests/test_stats.py
git -C /home/hamsa commit -m "$(cat <<'EOF'
feat: throughput counter (vanity.stats.StatsPrinter)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: xrp_vanity_gpu.py CLI — phase-v1 serial loop

**Files:**
- Create: `xrp_vanity_gpu/xrp_vanity_gpu.py`
- Create: `xrp_vanity_gpu/tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

`xrp_vanity_gpu/tests/test_cli.py`:
```python
"""CLI tests: pattern validation, deterministic-seed e2e, integration smoke.

The e2e test uses --seed-rng-seed + --max-matches to make a tiny
deterministic run.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "xrp_vanity_gpu.py"

# Use conda run wrapper so the test runs in rapids-23.12.
def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = [
        "bash", "-lc",
        "source ~/miniconda3/etc/profile.d/conda.sh && "
        "conda run -n rapids-23.12 python " + str(CLI) + " " +
        " ".join(args),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def test_cli_rejects_illegal_pattern():
    # '0' is not in the XRPL base58 alphabet.
    res = _run(["0Foo", "--batch-size", "16", "--max-matches", "0"])
    assert res.returncode != 0
    assert "illegal" in (res.stdout + res.stderr).lower() or \
           "alphabet" in (res.stdout + res.stderr).lower()


@pytest.mark.gpu
@pytest.mark.slow
def test_cli_finds_one_char_prefix_match():
    # A single-char prefix should match almost immediately.
    res = _run([
        "D",
        "--batch-size", "1024",
        "--max-matches", "1",
        "--seed-rng-seed", "12345",
        "--stats-interval", "1",
    ], timeout=60)
    assert res.returncode == 0, res.stderr
    assert "MATCH" in res.stdout
    assert "  rD" in res.stdout  # address line begins with rD


@pytest.mark.gpu
@pytest.mark.slow
def test_cli_finds_match_matches_xrplpy():
    # Same as above but parse the match line and verify against xrpl-py.
    res = _run([
        "D",
        "--batch-size", "1024",
        "--max-matches", "1",
        "--seed-rng-seed", "99",
    ], timeout=60)
    assert res.returncode == 0, res.stderr
    line = next(l for l in res.stdout.splitlines() if "MATCH" in l)
    # MATCH  rDxxx  seed=sEdYYY  (attempt N)
    parts = line.split()
    address = parts[2]
    seed_b58 = parts[3].split("=", 1)[1]
    from xrpl.core.keypairs import derive_keypair, derive_classic_address
    pub_hex, _ = derive_keypair(seed_b58)
    assert derive_classic_address(pub_hex) == address
```

- [ ] **Step 2: Run, see it fail**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_cli.py -v
```

- [ ] **Step 3: Implement xrp_vanity_gpu.py**

`xrp_vanity_gpu/xrp_vanity_gpu.py`:
```python
#!/usr/bin/env python
"""GPU-accelerated XRPL vanity address search.

Usage:
    python xrp_vanity_gpu.py PATTERN [options]

PATTERN matches the prefix of the address immediately after the leading 'r'.
"""
import argparse
import datetime as dt
import signal
import sys

import numpy as np

from vanity import encoding, gpu, sieve, stats


def _legal_pattern_chars(case_sensitive: bool) -> set[str]:
    alpha = encoding.XRPL_ALPHABET.decode("ascii")
    if case_sensitive:
        return set(alpha)
    return set(alpha.lower()) | set(alpha.upper())


def _validate_pattern(pattern: str, case_sensitive: bool) -> None:
    legal = _legal_pattern_chars(case_sensitive)
    bad = [c for c in pattern if c not in legal]
    if bad:
        legal_sorted = "".join(sorted(legal))
        sys.exit(
            f"error: illegal character(s) in PATTERN: {bad!r}\n"
            f"legal characters (alphabet, case-{'sensitive' if case_sensitive else 'insensitive'}):\n"
            f"  {legal_sorted}"
        )


def _emit_match(m: sieve.Match, out_fh) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] MATCH  {m.address}  seed={m.seed_b58}  (attempt {m.attempt:,})"
    print(line, flush=True)
    if out_fh is not None:
        out_fh.write(line + "\n")
        out_fh.flush()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pattern")
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--batch-size", type=int, default=1_048_576)
    p.add_argument("--out", type=str, default=None)
    p.add_argument("--max-matches", type=int, default=0,
                   help="0 = run until Ctrl-C")
    p.add_argument("--stats-interval", type=float, default=5.0)
    p.add_argument("--seed-rng-seed", type=int, default=None)
    args = p.parse_args()

    _validate_pattern(args.pattern, args.case_sensitive)

    rng = np.random.default_rng(args.seed_rng_seed)
    g = gpu.VanityGpu(batch_size=args.batch_size)
    sp = stats.StatsPrinter(interval_sec=args.stats_interval)

    out_fh = open(args.out, "a") if args.out else None
    stopping = False

    def _on_sigint(_signo, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, _on_sigint)

    attempt = 0
    matches_found = 0
    try:
        while not stopping:
            seeds = rng.bytes(args.batch_size * 16)
            pubkeys = g.run_batch(seeds)
            hits = sieve.sieve_batch(
                pubkeys=pubkeys,
                seeds=seeds,
                pattern=args.pattern,
                case_sensitive=args.case_sensitive,
                first_attempt_index=attempt,
            )
            for h in hits:
                _emit_match(h, out_fh)
                sp.add_match()
                matches_found += 1
                if args.max_matches and matches_found >= args.max_matches:
                    stopping = True
                    break
            attempt += args.batch_size
            line = sp.tick(processed=args.batch_size)
            if line is not None:
                print(line, flush=True)
    finally:
        if out_fh is not None:
            out_fh.close()
        final = sp.tick(processed=0, force_emit=True)
        if final:
            print(final, flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests, iterate to green**

```
conda run -n rapids-23.12 pytest xrp_vanity_gpu/tests/test_cli.py -v
```

If `test_cli_finds_one_char_prefix_match` times out at the default batch size, the user can lower `--batch-size` further. If it OOMs, lower the test's batch size argument too.

- [ ] **Step 5: Manual smoke run for `Daimyo`**

Run for ~30 seconds to confirm throughput and that the CLI doesn't crash:
```
conda run -n rapids-23.12 timeout 30 python xrp_vanity_gpu/xrp_vanity_gpu.py Daimyo \
  --batch-size 1048576 --stats-interval 5 --max-matches 1 || true
```
Expected: at least one stats line printed showing >= 1M/s. (Finding a `Daimyo` match in 30s is statistically possible but not expected.)

- [ ] **Step 6: Update xrp_vanity_gpu/README.md to reflect MVP shipped**

Replace the existing status table with:
```markdown
## Status (post-MVP, <DATE>)

| Component | Path | Status |
|---|---|---|
| GPU SHA-512 | `kernels/sha_kernels.cu` | PASS |
| GPU Ed25519 (donna port) | `kernels/ed25519_kernel.cu` | PASS — 1000 vectors |
| GPU pipeline (seed -> pubkey) | `kernels/pipeline_kernel.cu` | PASS — 1000 vs xrpl-py |
| CPU sieve (hash/base58/match) | `vanity/sieve.py` | PASS |
| CLI | `xrp_vanity_gpu.py` | Phase-v1 serial |

Run a search:
```
source ~/miniconda3/etc/profile.d/conda.sh && conda run -n rapids-23.12 \
  python xrp_vanity_gpu.py Daimyo --batch-size 1048576
```

See `docs/superpowers/specs/2026-05-20-xrp-vanity-gpu-design.md` for design.
```

- [ ] **Step 7: Commit**

```
git -C /home/hamsa add \
  xrp_vanity_gpu/xrp_vanity_gpu.py \
  xrp_vanity_gpu/tests/test_cli.py \
  xrp_vanity_gpu/README.md
git -C /home/hamsa commit -m "$(cat <<'EOF'
feat: vanity CLI (phase-v1 serial) — MVP shipped

xrp_vanity_gpu.py PATTERN [options] runs the full hybrid pipeline:
GPU sha512 + ed25519 -> pubkey; CPU sha256 + ripemd160 + base58check
+ prefix match. Validated end-to-end against xrpl-py for one-char
prefix matches.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13 (optimization): Phase-v2 double-buffered streams

**Files:**
- Modify: `xrp_vanity_gpu/vanity/gpu.py`
- Modify: `xrp_vanity_gpu/xrp_vanity_gpu.py`
- Create: `xrp_vanity_gpu/tests/test_gpu_double_buffer.py`

Skip this task if MVP throughput is already adequate for the user's first real search.

- [ ] **Step 1: Write a perf-regression test**

```python
"""Verify double-buffered runner yields the SAME output as serial."""
import secrets
import pytest

from vanity import gpu


@pytest.mark.gpu
def test_double_buffer_output_matches_serial():
    B = 1024
    seeds = b"".join(secrets.token_bytes(16) for _ in range(B))
    serial = gpu.VanityGpu(batch_size=B).run_batch(seeds)

    db = gpu.VanityGpuDoubleBuffered(batch_size=B)
    out = db.run_batch_and_wait(seeds)
    assert out == serial
```

- [ ] **Step 2: Implement `VanityGpuDoubleBuffered`**

Add to `vanity/gpu.py`:
```python
class VanityGpuDoubleBuffered:
    """Two CUDA streams alternating to overlap kernel with host transfer.

    Workflow:
        submit(seeds_A)        # kicks off stream A
        submit(seeds_B)        # kicks off stream B while A is running
        out_A = collect()      # waits for A, returns pubkeys
        submit(seeds_C)        # kicks off C using A's freed slot
        out_B = collect()      # waits for B
        ...

    For a simpler API we also provide a synchronous run_batch_and_wait
    that just runs one batch through the producer/consumer plumbing
    (mostly useful for tests).
    """
    # (Implementation: two device-buffer slots, two streams, two pinned host
    # buffers. submit() launches on the next-free slot; collect() waits on
    # the oldest in-flight slot.)
    ...
```

Implementation detail: use `cupy.cuda.Stream(non_blocking=True)`, two slots, and `Event.record()` on each kernel launch. `collect()` synchronises on the oldest event then memcpys the pubkeys back.

- [ ] **Step 3: Wire the CLI to use double buffering**

Replace the inner loop in `xrp_vanity_gpu.py`:
```python
db = gpu.VanityGpuDoubleBuffered(batch_size=args.batch_size)
# Prime the pipeline
seeds_a = rng.bytes(args.batch_size * 16)
db.submit(seeds_a)
while not stopping:
    seeds_next = rng.bytes(args.batch_size * 16)
    db.submit(seeds_next)
    seeds_done, pubkeys = db.collect()  # blocks on oldest stream
    hits = sieve.sieve_batch(pubkeys, seeds_done, args.pattern,
                             args.case_sensitive, attempt)
    # ... emit, tick stats ...
    attempt += args.batch_size
# Drain the last in-flight batch on Ctrl-C
seeds_done, pubkeys = db.collect()
hits = sieve.sieve_batch(...)  # ... emit ...
```

- [ ] **Step 4: Benchmark uplift**

Time 30 seconds of `Daimyo` search with each runner:
```
conda run -n rapids-23.12 timeout 30 python xrp_vanity_gpu.py Daimyo \
  --batch-size 1048576 --serial-only 2>&1 | tail -3
conda run -n rapids-23.12 timeout 30 python xrp_vanity_gpu.py Daimyo \
  --batch-size 1048576 2>&1 | tail -3
```
Expected: double-buffered shows ≥1.5× the avg/s of serial. If less, profile with `nsys`.

- [ ] **Step 5: Commit**

```
git -C /home/hamsa add xrp_vanity_gpu/vanity/gpu.py \
  xrp_vanity_gpu/xrp_vanity_gpu.py \
  xrp_vanity_gpu/tests/test_gpu_double_buffer.py
git -C /home/hamsa commit -m "$(cat <<'EOF'
perf: double-buffered CUDA streams overlap kernel with host transfer

Two stream slots, pinned host buffers per slot. Output bit-identical
to serial path. Benchmark uplift: <FILL IN> -> <FILL IN> M/s.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14 (tuning): Batch-size sweep and final throughput report

- [ ] **Step 1: Sweep `--batch-size` over {64K, 256K, 1M, 4M}**

```
for BS in 65536 262144 1048576 4194304; do
  echo "=== batch_size=$BS ==="
  conda run -n rapids-23.12 timeout 30 python xrp_vanity_gpu.py D \
    --batch-size $BS --max-matches 0 --stats-interval 5 2>&1 | tail -4
done
```
Record max-throughput batch size.

- [ ] **Step 2: Update `xrp_vanity_gpu/README.md` with measured numbers**

Add a "Performance" section noting the optimal batch size and the achieved candidates/sec on the 2060 Super.

- [ ] **Step 3: Commit**

```
git -C /home/hamsa add xrp_vanity_gpu/README.md
git -C /home/hamsa commit -m "$(cat <<'EOF'
docs: record final throughput numbers and tuned batch size

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Out-of-band caveats

- **Subagent Bash limitation:** subagents in this environment cannot run Bash (they hang waiting for permission). All shell steps must run in the main session. Reference: `[[feedback-subagent-bash]]`.
- **Conda activation:** every command that needs Python deps must source the conda profile. The bash-lc wrapper in `tests/test_cli.py` exists for this reason.
- **NVRTC source must be ASCII:** non-ASCII characters (em-dashes, smart quotes) anywhere in the kernel source strings will break compilation silently. Use ASCII-only.
- **Scalar args to kernels:** pass `np.uint32(N)`, never `cp.array(N)`. The latter raises `cudaErrorIllegalAddress`.
- **donna license:** ed25519-donna is public-domain (CC0). No license-tracking required, but `third_party/README.md` notes the pinned commit.
- **xrpl-py oracle:** every layer is validated against `xrpl-py` rather than our own code. This is deliberate — we never trust our own re-derivation of XRPL semantics.
