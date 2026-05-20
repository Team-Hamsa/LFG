# xrp_vanity_gpu

GPU-accelerated XRP vanity address search (work in progress). Target CLI:
`python xrp_vanity_gpu.py PATTERN [--case-sensitive] [--threads N]`

Run in `rapids-23.12` conda env:
`source ~/miniconda3/etc/profile.d/conda.sh && conda run -n rapids-23.12 python ...`

## Status (2026-05-20)

| Component | File | Status |
|---|---|---|
| SHA-512 (CUDA NVRTC) | `kernels/sha_kernels.cu` | PASS |
| SHA-256 (CUDA NVRTC) | `kernels/sha_kernels.cu` | **FAIL — see debug/** |
| Base58 + icase match | `kernels/base58_kernel.cu` | PASS |
| RIPEMD-160 | `kernels/ripemd160_kernel.cu` | Untested |
| Ed25519 scalarmult | — | NOT WRITTEN |
| `xrp_vanity_gpu.py` CLI | — | NOT WRITTEN |

## SHA-256 bug

GPU kernel AND every from-scratch pure-Python rewrite produce
`7aa2b8f31bdc8f35...` for `sha256(b'abc')` instead of the correct
`ba7816bf8f01cfea...`. OpenSSL, hashlib, sha256sum, pycryptodome,
and `libcrypto.SHA256_Transform` all agree on the correct answer.

Verified correct in mine: K constants, W schedule (W[16..18] match FIPS B.2),
round 0 transition, round 1 `a` value, sigma rotations, Ch/Maj, padding,
endianness. Yet final hash diverges. Magic offset at round 1 T1: `0x82439887`.

See `../.claude/projects/-home-hamsa/memory/project_xrp_vanity_gpu.md` for
the full debug history.

## Layout

```
kernels/   CUDA device code (NVRTC-compatible, ASCII-only, no #include)
tests/     Functional tests against xrpl-py / hashlib vectors
debug/     Step-by-step bug isolation scripts for the SHA-256 mystery
```

## NVRTC gotchas

- Prepend PREAMBLE with `typedef unsigned int uint32_t;` (no stdint.h)
- No non-ASCII characters in source strings (em-dashes break compilation)
- Scalar kernel args: pass `np.uint32(N)`, NOT `cp.array(N)` — the latter
  causes `cudaErrorIllegalAddress`
