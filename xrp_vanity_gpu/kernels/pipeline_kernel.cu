/* pipeline: seed (16B) -> XRPL Ed25519 pubkey (33B).

   XRPL derivation (matches xrpl-py derive_keypair / ed25519-donna):
     pre   = SHA512(seed16)[:32]            (the 32-byte root private key)
     extsk = SHA512(pre)[:32]               (Ed25519 expanded scalar half)
     clamp: extsk[0] &= 248; extsk[31] = (extsk[31] & 127) | 64
     A     = scalarmult_base(extsk)
     pubkey = 0xED || pack(A)

   Depends on sha512_16/sha512_32 (sha_kernels.cu) and the Ed25519
   functions + base table (ed25519_kernel.cu); all share one translation
   unit after concatenation in vanity/gpu.py. */

extern "C" __global__
void pipeline(
    const unsigned char * __restrict__ seeds,    /* B * 16 */
    unsigned char * __restrict__ pubkeys,        /* B * 33 */
    unsigned int B
) {
    unsigned int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= B) return;

    uint8_t pre[64];
    sha512_16(seeds + i * 16, pre);

    uint8_t extsk[64];
    sha512_32(pre, extsk);

    extsk[0] &= 248;
    extsk[31] = (extsk[31] & 127) | 64;

    bignum256modm a;
    ge25519 A;
    expand256_modm(a, extsk, 32);
    ge25519_scalarmult_base_niels(&A, ge25519_niels_base_multiples, a);

    pubkeys[i * 33] = 0xED;
    ge25519_pack(pubkeys + i * 33 + 1, &A);
}
