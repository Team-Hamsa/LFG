# XRP Vanity GPU — Design

**Date:** 2026-05-20
**Repo:** `/home/hamsa/xrp_vanity_gpu/`
**Status:** Approved design, pending implementation plan.

## Goal

Build a reusable CUDA-accelerated CLI that finds XRPL Ed25519 addresses
matching a user-supplied prefix. Target hardware: RTX 2060 Super (8 GB,
CUDA 12.2). Runtime: `rapids-23.12` conda env with CuPy 13.4.1.

Target CLI:
```
python xrp_vanity_gpu.py PATTERN [options]
```

First real-world use: prefix `Daimyo`.

## Non-Goals

- Secp256k1 (`s...` family-seed type 0x21) keys — Ed25519 only for v1.
- secp256k1 cold-wallet vanity tools.
- Substring or regex matching — prefix-only.
- Distributed multi-GPU search.

## Architecture

Producer / consumer pipeline in a single Python process.

```
[host RNG] --B*16B--> [GPU pipeline kernel] --B*33B--> [host pinned] --> [CPU sieve] --> matches
                                                                              |
                                                                         (hashlib +
                                                                          base58 +
                                                                          prefix check)
```

**GPU does the math-heavy stages:**
1. SHA-512 of each 16-byte seed.
2. Take first 32 bytes of the SHA-512 digest as the Ed25519 scalar
   (no clamping — this matches XRPL's `derive_keypair` convention).
3. Ed25519 scalar multiplication of the base point.
4. Compress the resulting Edwards point to 32 bytes; prepend `0xED`
   to get the 33-byte XRPL Ed25519 public key.

**CPU does the cheap stages** using stdlib `hashlib`:
5. `RIPEMD160(SHA256(pubkey))` → 20-byte account_id.
6. `0x00 || account_id || double_sha256[:4]` → 25 bytes.
7. Base58 encode with the XRPL alphabet
   `rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz`.
8. Check whether characters at positions 1..len(PATTERN)+1 match PATTERN
   (case-insensitive unless `--case-sensitive`).
9. On match: re-encode the original 16-byte seed as a family-seed string
   (`sEd…`) and emit a match line.

The hybrid split is deliberate: it sidesteps the unsolved GPU SHA-256 bug
(see `archive/README.md`) by never running SHA-256 on the GPU.

## File Layout

```
kernels/
  sha_kernels.cu          # SHA-512 only (already passing). SHA-256 device fns archived.
  ed25519_kernel.cu       # NEW — ed25519-donna port: fe_*, ge_*, scalar_mult_base, compress.
  pipeline_kernel.cu      # NEW — composite: seed -> sha512[:32] -> scalar_mult -> compress.
vanity/
  __init__.py
  gpu.py                  # CuPy module loader, kernel launch wrappers.
  sieve.py                # CPU sha256+ripemd160+base58+prefix match.
  encoding.py             # seed -> sEd... and 25B payload -> r... helpers.
  stats.py                # throughput counters, periodic stats lines.
xrp_vanity_gpu.py         # CLI entry.
tests/
  test_ed25519_vectors.py # GPU output vs ed25519-donna C reference vectors.
  test_pipeline_e2e.py    # Full GPU+CPU pipeline vs xrpl-py.derive_keypair.
  test_sieve.py           # Hand-crafted prefix matches.
archive/
  README.md               # Why each archived artifact is parked.
  base58_kernel.cu        # Was passing on GPU; superseded by hybrid arch.
  ripemd160_kernel.cu     # Untested; superseded by hybrid arch.
  sha256_*.py             # The SHA-256 debug scripts (formerly debug/).
docs/
  superpowers/specs/2026-05-20-xrp-vanity-gpu-design.md   # this file
README.md                 # Updated status table after archive.
```

## Components

### `kernels/ed25519_kernel.cu`

NVRTC-compatible CUDA port of [ed25519-donna](https://github.com/floodyberry/ed25519-donna),
restricted to the `scalar_mult_base + compress` path. We do **not** port
sign/verify — vanity search only needs `B^k` where `B` is the base point.

- Field element representation: 5 × 51-bit limbs in `uint64_t[5]`
  (matches donna's `bignum25519`).
- Group element: extended Edwards coordinates (`ge_p3`).
- Scalar mult: precomputed base-point table, signed-radix-16 decomposition
  (donna's `ge_scalarmult_base`).
- Multiplication needs the high 64 bits of a 64×64 product:
  use `__umul64hi` instead of donna's `__uint128_t`.
- One CUDA thread per candidate scalar. Block size 128 or 256
  (tunable; pick after a quick microbench).
- NVRTC preamble: `typedef unsigned int uint32_t; typedef unsigned long long uint64_t;`
  and ASCII-only source (per [[project-xrp-vanity-gpu]] gotchas).
- Scalar kernel args must be `np.uint32(N)` not `cp.array(N)`.

### `kernels/pipeline_kernel.cu`

Thin composite kernel. For each thread `i`:
1. Read `seed[i*16 .. i*16+16]`.
2. Call device SHA-512, take first 32 bytes.
3. Call `scalar_mult_base` from `ed25519_kernel.cu`.
4. Call `point_compress` from `ed25519_kernel.cu`.
5. Write `0xED` then the 32 compressed bytes to `out[i*33 .. i*33+33]`.

Kernel signature (CuPy `RawModule`):
```
__global__ void pipeline(
    const unsigned char* __restrict__ seeds,   // B * 16
    unsigned char* __restrict__ pubkeys,       // B * 33
    unsigned int B
);
```

### `vanity/gpu.py`

- Builds the kernel source string at Python level by concatenating
  the NVRTC preamble, `sha_kernels.cu`, `ed25519_kernel.cu`, and
  `pipeline_kernel.cu` (in that order) before passing to
  `cupy.RawModule`. NVRTC `#include` requires a header search path
  which we avoid.
- Exposes `class VanityGpu` with:
  - `__init__(batch_size: int)` — allocates pinned host buffer and device buffers.
  - `run_batch(seeds: bytes) -> bytes` — returns 33B-per-candidate output.

### `vanity/sieve.py`

Pure Python, no GPU dependency, testable in isolation.

- `address_from_pubkey(pubkey33: bytes) -> str` — sha256 -> ripemd160 ->
  base58check with prefix byte `0x00`.
- `match(address: str, pattern: str, case_sensitive: bool) -> bool` — checks
  `address[1 : 1 + len(pattern)]` (case-handled).
- `sieve_batch(pubkeys: bytes, pattern: str, case_sensitive: bool,
   seeds: bytes) -> list[Match]` — iterates the batch, returns hits.

`Match` is a `NamedTuple` of `(seed_b58: str, address: str, attempt: int)`.

### `vanity/encoding.py`

- `XRPL_ALPHABET = b"rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"`.
- `b58encode(data: bytes) -> str` — XRPL-alphabet base58 (no checksum).
- `family_seed_encode(seed16: bytes) -> str` — version `0x01 0xE1 0x4B`
  (Ed25519 family seed) || seed || double-sha256[:4], then b58.
- `address_encode(account_id20: bytes) -> str` — `0x00` || account_id
  || double-sha256[:4], then b58.

### `vanity/stats.py`

A `StatsPrinter` that runs on a wall-clock interval, prints
`candidates/sec`, rolling average, total processed, matches found,
elapsed time. Uses `time.monotonic()`; no external deps.

### `xrp_vanity_gpu.py` (CLI)

```
python xrp_vanity_gpu.py PATTERN [options]

PATTERN                   Prefix to match immediately after 'r'.

--case-sensitive          Strict character match.
                          Default: case-insensitive.
--batch-size N            Candidates per GPU batch.
                          Default: 1_048_576.
--out FILE                Append matches to FILE in addition to stdout.
--max-matches N           Stop after N matches.
                          Default: unlimited (Ctrl-C to stop).
--stats-interval SEC      Print throughput every SEC seconds.
                          Default: 5.
--seed-rng-seed N         Deterministic RNG seed for reproducible tests.
```

Stdout match line:
```
[2026-05-20 18:42:13] MATCH  rDaimyoXXXXX...  seed=sEd...  (attempt 12,345,678)
```

Stdout stats line:
```
[2026-05-20 18:42:10] 4.2M/s  avg 4.1M/s  total 41.0M  matches 0  elapsed 10s
```

## Data Flow & Memory Budget

| Buffer                    | Size at B=1M | Location         |
|---------------------------|--------------|------------------|
| seeds                     | 16 MB        | device input     |
| sha512 outputs (scratch)  | 64 MB        | device           |
| scalars (sha512[:32])     | 32 MB        | device           |
| compressed pubkeys        | 33 MB        | device → host    |
| host pubkey buffer        | 33 MB        | pinned host      |

Total device footprint per batch ≈ 150 MB. Fits 50× over in 8 GB.

Host RNG cost is trivial (`numpy.random.default_rng().bytes` runs at
multi-GB/s). We do **not** generate seeds on-device — GPU RNG quality
is harder to reason about and seed gen is not the bottleneck.

## Concurrency Phases

**Phase v1 — serial (correctness-first):**
```
loop:
  seeds = rng.bytes(B*16)
  pubkeys = gpu.run_batch(seeds)        # cuda.synchronize before return
  hits = sieve_batch(pubkeys, pattern, ...)
  for h in hits: emit(h)
```
GPU and CPU idle each other half the time. Trivially debuggable.

**Phase v2 — double-buffered streams:**
Two CUDA streams, a Python `threading.Thread` for the sieve, one
ring-buffer slot per stream. While stream A is being sieved on CPU,
stream B is computing on GPU. Expected ≈ 2× throughput uplift.

Phase v2 is purely an optimization. The MVP is phase v1.

## Throughput Estimate

A 2060 Super has ~30 SMs / 2176 CUDA cores at ~1.65 GHz. A
straightforward donna scalar-mult-base on one thread per candidate
should land in the **1–5 M candidates/sec** range. For `PATTERN=Daimyo`
(6 chars, case-insensitive, alphabet has 35 distinct chars after
case-fold), expected attempts ≈ 35⁶ ≈ 1.8×10⁹. Wall-clock time to
first match: minutes to low-hours.

These are projections; the implementation plan will treat anything
≥ 1 M/sec as acceptable for shipping the MVP, with phase v2 as the
optimization milestone.

## Error Handling

- **Preflight:** import CuPy, `cupy.cuda.runtime.getDeviceCount() > 0`,
  compile both kernels. On NVRTC failure, surface the compiler log with
  line numbers and exit non-zero.
- **Pattern validation:** every character in `PATTERN` must be in the
  XRPL base58 alphabet (case-insensitive set). Otherwise refuse with
  the legal-char list.
- **Ctrl-C:** cancel current batch, flush match buffer to `--out`,
  print final stats, exit 0.
- **CUDA runtime error mid-run:** print device-sync status, flush
  match buffer, exit non-zero.

## Testing Strategy

Three layers, implemented in order:

1. **Ed25519 kernel vs ed25519-donna C reference.** Build donna once
   as a shared library, generate 1000 `(scalar, point_compressed)`
   vectors, dump to `tests/data/ed25519_vectors.json`. The GPU kernel
   must reproduce every byte for every vector.

2. **Pipeline kernel vs xrpl-py.** For 1000 random 16-byte seeds,
   `pipeline_kernel`'s 33-byte output must equal the public-key bytes
   from `xrpl.core.keypairs.derive_keypair(family_seed_encode(seed))`.

3. **End-to-end vs xrpl-py.** For 10 000 random seeds, every
   GPU-derived address must equal `xrpl-py`'s address derivation.
   Plus: hand-craft a seed that addresses to a known `rDa…` prefix and
   confirm the sieve catches it on first pass.

`tests/test_sieve.py` exercises `vanity/sieve.py` with hand-built
pubkeys; no GPU needed.

All tests run under:
`source ~/miniconda3/etc/profile.d/conda.sh && conda run -n rapids-23.12 pytest tests/`.

## Implementation Phasing

This is what the implementation plan will follow:

1. Move obsolete artifacts into `archive/`. Write `archive/README.md`.
   Trim `sha_kernels.cu` to SHA-512 only.
2. Build C-side donna shared lib + Python vector dumper.
   Commit `tests/data/ed25519_vectors.json`.
3. Port donna field arithmetic (`fe_*`) into `ed25519_kernel.cu`.
   Unit-test field ops in isolation against donna.
4. Port donna group arithmetic (`ge_*`) and `scalar_mult_base`.
   Run layer-1 test (1000 vectors).
5. Compose `pipeline_kernel.cu` from SHA-512 + Ed25519 + compress.
   Run layer-2 test (1000 seeds vs xrpl-py).
6. Build `vanity/sieve.py` + `vanity/encoding.py`. Test in isolation.
7. Wire `xrp_vanity_gpu.py` CLI in phase-v1 serial mode.
   Run layer-3 end-to-end test (10 000 seeds vs xrpl-py).
   Run a real search for `Daimyo` to first match.
8. Add phase-v2 double-buffered streams. Benchmark uplift.
9. Tune batch size; record final candidates/sec in README.

MVP = steps 1–7. Steps 8–9 are optimization.

## Open Questions

None blocking. All design decisions are locked.

## References

- ed25519-donna: https://github.com/floodyberry/ed25519-donna
- XRPL address encoding: https://xrpl.org/accounts.html#address-encoding
- XRPL base58 alphabet: see `vanity/encoding.py`.
- Existing repo notes: `README.md`, `archive/README.md`,
  `~/.claude/projects/-home-hamsa/memory/project_xrp_vanity_gpu.md`.
