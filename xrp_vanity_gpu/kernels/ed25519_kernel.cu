/* Ed25519 kernel - donna port. Populated incrementally in Tasks 7-8.

   Conventions:
   - Field element (bignum25519): 5 x 51-bit limbs in uint64_t[5], little-endian.
   - Group element (ge25519): extended Edwards coords (X, Y, Z, T), each bignum25519.
   - Multiply needs 64x64->128: use __umul64hi for the high 64 bits.
   - All functions are __device__ __forceinline__; no host-side calls.
   - Compiled as part of the concatenated NVRTC source in vanity/gpu.py.

   See third_party/ed25519-donna/ for the reference C implementation. */

/* Placeholder: field and group arithmetic arrive in Tasks 7-8.
   Until then the module compiles cleanly with only sha_kernels.cu. */
