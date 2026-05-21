# third_party

## ed25519-donna

Source: https://github.com/floodyberry/ed25519-donna
License: Public domain (CC0). See file headers.

Pinned commit: 8757bd4cd209cb032853ece0ce413f122eef212c

We use the 64-bit code paths:
- `curve25519-donna-64bit.h` for field arithmetic (fe_* / `bignum25519`).
- `ed25519-donna.h` for group ops and `ge_scalarmult_base`.
- `ed25519-donna-basepoint-table.h` for the precomputed base table.
- `modm-donna-64bit.h` for scalar (mod L) arithmetic.

These are referenced (not modified) for the CUDA port in
`kernels/ed25519_kernel.cu`. The donna C source is also built as a
static library by `tools/Makefile` and used to generate test vectors
in `tests/data/ed25519_vectors.json`.
