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
