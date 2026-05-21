# xrp_vanity_gpu

GPU-accelerated XRP (Ed25519) vanity address search. The GPU derives public
keys from random seeds; the CPU sieves them for prefix matches across all cores.

## Usage

```bash
source ~/miniconda3/etc/profile.d/conda.sh
conda run -n rapids-23.12 python xrp_vanity_gpu.py PATTERN [options]
```

`PATTERN` matches the address characters immediately after the leading `r`
(e.g. `Daimyo` matches `rDaimyo...`).

| Option | Default | Meaning |
|---|---|---|
| `--case-sensitive` | off | exact-case prefix match |
| `--batch-size N` | 1048576 | seeds derived per GPU launch |
| `--workers N` | 0 (all cores) | CPU sieve processes |
| `--max-matches N` | 0 (until Ctrl-C) | stop after N hits |
| `--out FILE` | — | append matches to FILE |
| `--stats-interval S` | 5.0 | seconds between throughput lines |
| `--seed-rng-seed N` | random | deterministic run (testing) |

Only the base58 alphabet is legal in `PATTERN`
(`rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz`); `--case-sensitive`
off also accepts upper/lowercase variants.

## Architecture: hybrid GPU/CPU

- **GPU** (`kernels/`, `vanity/gpu.py`): seed16 -> double SHA-512 + Ed25519 clamp
  -> scalar-mult base point -> packed 33-byte pubkey. Compiled once via CuPy NVRTC.
- **CPU** (`vanity/sieve.py`): SHA-256 + RIPEMD-160 -> account_id -> base58check
  address -> prefix match, fanned across all cores by `ParallelSieve`.

The CPU sieve is the throughput bottleneck (pure-Python base58check), so it runs
in a persistent multiprocessing pool while the GPU pipeline stays well ahead.

## Throughput (RTX 2060 Super, 20-core CPU)

| Stage | Rate |
|---|---|
| GPU pipeline | ~4.6M seeds/s |
| CPU sieve, single core | ~31K/s |
| CPU sieve, 20 cores | ~283K/s |
| End-to-end CLI | ~280-310K/s |

~3x the ~100K/s Java CPU baseline.

## Layout

```
kernels/      CUDA device code (NVRTC-compatible, ASCII-only, no #include)
vanity/       encoding.py, sieve.py, gpu.py, stats.py
tests/        functional tests vs xrpl-py / hashlib / donna vectors
third_party/  vendored ed25519-donna (reference + vector generator)
archive/      parked all-GPU SHA-256 experiment (see archive/README.md)
```

## NVRTC gotchas

- Prepend a PREAMBLE with `typedef unsigned int uint32_t;` (no `stdint.h`).
- ASCII-only source (em-dashes and other non-ASCII break compilation).
- Pass scalar kernel args as `np.uint32(N)`, not `cp.array(N)` — the latter
  causes `cudaErrorIllegalAddress`.
