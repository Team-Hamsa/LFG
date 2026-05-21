/* Ed25519 kernel - donna port. Field arithmetic (Task 7); group ops (Task 8).

   Conventions:
   - Field element (bignum25519): 5 x 51-bit limbs in uint64_t[5], little-endian.
   - Group element (ge25519): extended Edwards coords (X, Y, Z, T), each bignum25519.
   - Multiply needs 64x64->128: use __umul64hi for the high 64 bits.
   - All functions are __device__; no host-side calls.
   - Compiled as part of the concatenated NVRTC source in vanity/gpu.py.

   Field code transliterated from third_party/ed25519-donna/curve25519-donna-64bit.h
   and curve25519-donna-helpers.h. The donna 128-bit accumulator macros
   (from ed25519-donna-portable.h) are reimplemented here on top of __umul64hi. */

typedef uint64_t bignum25519[5];

/* 128-bit accumulator as an explicit (hi, lo) pair. Mirrors donna's
   struct-uint128_t fallback macros, with mul64x64_128 built on __umul64hi. */
typedef struct cu_uint128 { uint64_t lo; uint64_t hi; } uint128_t;

#define mul64x64_128(out, a, b) { (out).lo = (a) * (b); (out).hi = __umul64hi((a), (b)); }
#define add128(a, b) { uint64_t _p = (a).lo; (a).lo += (b).lo; (a).hi += (b).hi + ((a).lo < _p); }
#define add128_64(a, b) { uint64_t _p = (a).lo; (a).lo += (b); (a).hi += ((a).lo < _p); }
#define lo128(a) ((a).lo)
/* shift in [1,63]: combine hi/lo without forming a real 128-bit value. */
#define shr128(out, in, shift) (out) = (((in).hi << (64 - (shift))) | ((in).lo >> (shift)));
#define shl128(out, in, shift) (out) = (((in).hi << (shift)) | ((in).lo >> (64 - (shift))));

__device__ static const uint64_t reduce_mask_51 = ((uint64_t)1 << 51) - 1;

/* multiples of p */
__device__ static const uint64_t twoP0      = 0x0fffffffffffda;
__device__ static const uint64_t twoP1234   = 0x0ffffffffffffe;
__device__ static const uint64_t fourP0     = 0x1fffffffffffb4;
__device__ static const uint64_t fourP1234  = 0x1ffffffffffffc;

/* out = in */
__device__ __forceinline__ static void
curve25519_copy(bignum25519 out, const bignum25519 in) {
    out[0] = in[0]; out[1] = in[1]; out[2] = in[2]; out[3] = in[3]; out[4] = in[4];
}

/* out = a + b */
__device__ __forceinline__ static void
curve25519_add(bignum25519 out, const bignum25519 a, const bignum25519 b) {
    out[0] = a[0] + b[0];
    out[1] = a[1] + b[1];
    out[2] = a[2] + b[2];
    out[3] = a[3] + b[3];
    out[4] = a[4] + b[4];
}

/* out = a + b, where a and/or b are the result of a basic op (add,sub) */
__device__ __forceinline__ static void
curve25519_add_after_basic(bignum25519 out, const bignum25519 a, const bignum25519 b) {
    out[0] = a[0] + b[0];
    out[1] = a[1] + b[1];
    out[2] = a[2] + b[2];
    out[3] = a[3] + b[3];
    out[4] = a[4] + b[4];
}

__device__ __forceinline__ static void
curve25519_add_reduce(bignum25519 out, const bignum25519 a, const bignum25519 b) {
    uint64_t c;
    out[0] = a[0] + b[0]    ; c = (out[0] >> 51); out[0] &= reduce_mask_51;
    out[1] = a[1] + b[1] + c; c = (out[1] >> 51); out[1] &= reduce_mask_51;
    out[2] = a[2] + b[2] + c; c = (out[2] >> 51); out[2] &= reduce_mask_51;
    out[3] = a[3] + b[3] + c; c = (out[3] >> 51); out[3] &= reduce_mask_51;
    out[4] = a[4] + b[4] + c; c = (out[4] >> 51); out[4] &= reduce_mask_51;
    out[0] += c * 19;
}

/* out = a - b */
__device__ __forceinline__ static void
curve25519_sub(bignum25519 out, const bignum25519 a, const bignum25519 b) {
    out[0] = a[0] + twoP0    - b[0];
    out[1] = a[1] + twoP1234 - b[1];
    out[2] = a[2] + twoP1234 - b[2];
    out[3] = a[3] + twoP1234 - b[3];
    out[4] = a[4] + twoP1234 - b[4];
}

/* out = a - b, where a and/or b are the result of a basic op (add,sub) */
__device__ __forceinline__ static void
curve25519_sub_after_basic(bignum25519 out, const bignum25519 a, const bignum25519 b) {
    out[0] = a[0] + fourP0    - b[0];
    out[1] = a[1] + fourP1234 - b[1];
    out[2] = a[2] + fourP1234 - b[2];
    out[3] = a[3] + fourP1234 - b[3];
    out[4] = a[4] + fourP1234 - b[4];
}

__device__ __forceinline__ static void
curve25519_sub_reduce(bignum25519 out, const bignum25519 a, const bignum25519 b) {
    uint64_t c;
    out[0] = a[0] + fourP0    - b[0]    ; c = (out[0] >> 51); out[0] &= reduce_mask_51;
    out[1] = a[1] + fourP1234 - b[1] + c; c = (out[1] >> 51); out[1] &= reduce_mask_51;
    out[2] = a[2] + fourP1234 - b[2] + c; c = (out[2] >> 51); out[2] &= reduce_mask_51;
    out[3] = a[3] + fourP1234 - b[3] + c; c = (out[3] >> 51); out[3] &= reduce_mask_51;
    out[4] = a[4] + fourP1234 - b[4] + c; c = (out[4] >> 51); out[4] &= reduce_mask_51;
    out[0] += c * 19;
}

/* out = -a */
__device__ __forceinline__ static void
curve25519_neg(bignum25519 out, const bignum25519 a) {
    uint64_t c;
    out[0] = twoP0    - a[0]    ; c = (out[0] >> 51); out[0] &= reduce_mask_51;
    out[1] = twoP1234 - a[1] + c; c = (out[1] >> 51); out[1] &= reduce_mask_51;
    out[2] = twoP1234 - a[2] + c; c = (out[2] >> 51); out[2] &= reduce_mask_51;
    out[3] = twoP1234 - a[3] + c; c = (out[3] >> 51); out[3] &= reduce_mask_51;
    out[4] = twoP1234 - a[4] + c; c = (out[4] >> 51); out[4] &= reduce_mask_51;
    out[0] += c * 19;
}

/* out = a * b */
__device__ __forceinline__ static void
curve25519_mul(bignum25519 out, const bignum25519 in2, const bignum25519 in) {
    uint128_t mul;
    uint128_t t[5];
    uint64_t r0,r1,r2,r3,r4,s0,s1,s2,s3,s4,c;

    r0 = in[0]; r1 = in[1]; r2 = in[2]; r3 = in[3]; r4 = in[4];
    s0 = in2[0]; s1 = in2[1]; s2 = in2[2]; s3 = in2[3]; s4 = in2[4];

    mul64x64_128(t[0], r0, s0)
    mul64x64_128(t[1], r0, s1) mul64x64_128(mul, r1, s0) add128(t[1], mul)
    mul64x64_128(t[2], r0, s2) mul64x64_128(mul, r2, s0) add128(t[2], mul) mul64x64_128(mul, r1, s1) add128(t[2], mul)
    mul64x64_128(t[3], r0, s3) mul64x64_128(mul, r3, s0) add128(t[3], mul) mul64x64_128(mul, r1, s2) add128(t[3], mul) mul64x64_128(mul, r2, s1) add128(t[3], mul)
    mul64x64_128(t[4], r0, s4) mul64x64_128(mul, r4, s0) add128(t[4], mul) mul64x64_128(mul, r3, s1) add128(t[4], mul) mul64x64_128(mul, r1, s3) add128(t[4], mul) mul64x64_128(mul, r2, s2) add128(t[4], mul)

    r1 *= 19; r2 *= 19; r3 *= 19; r4 *= 19;

    mul64x64_128(mul, r4, s1) add128(t[0], mul) mul64x64_128(mul, r1, s4) add128(t[0], mul) mul64x64_128(mul, r2, s3) add128(t[0], mul) mul64x64_128(mul, r3, s2) add128(t[0], mul)
    mul64x64_128(mul, r4, s2) add128(t[1], mul) mul64x64_128(mul, r2, s4) add128(t[1], mul) mul64x64_128(mul, r3, s3) add128(t[1], mul)
    mul64x64_128(mul, r4, s3) add128(t[2], mul) mul64x64_128(mul, r3, s4) add128(t[2], mul)
    mul64x64_128(mul, r4, s4) add128(t[3], mul)

                         r0 = lo128(t[0]) & reduce_mask_51; shr128(c, t[0], 51);
    add128_64(t[1], c)   r1 = lo128(t[1]) & reduce_mask_51; shr128(c, t[1], 51);
    add128_64(t[2], c)   r2 = lo128(t[2]) & reduce_mask_51; shr128(c, t[2], 51);
    add128_64(t[3], c)   r3 = lo128(t[3]) & reduce_mask_51; shr128(c, t[3], 51);
    add128_64(t[4], c)   r4 = lo128(t[4]) & reduce_mask_51; shr128(c, t[4], 51);
    r0 +=   c * 19; c = r0 >> 51; r0 = r0 & reduce_mask_51;
    r1 +=   c;

    out[0] = r0; out[1] = r1; out[2] = r2; out[3] = r3; out[4] = r4;
}

__device__ static void
curve25519_mul_noinline(bignum25519 out, const bignum25519 in2, const bignum25519 in) {
    curve25519_mul(out, in2, in);
}

/* out = in^(2 * count) */
__device__ static void
curve25519_square_times(bignum25519 out, const bignum25519 in, uint64_t count) {
    uint128_t mul;
    uint128_t t[5];
    uint64_t r0,r1,r2,r3,r4,c;
    uint64_t d0,d1,d2,d4,d419;

    r0 = in[0]; r1 = in[1]; r2 = in[2]; r3 = in[3]; r4 = in[4];

    do {
        d0 = r0 * 2;
        d1 = r1 * 2;
        d2 = r2 * 2 * 19;
        d419 = r4 * 19;
        d4 = d419 * 2;

        mul64x64_128(t[0], r0, r0) mul64x64_128(mul, d4, r1) add128(t[0], mul) mul64x64_128(mul, d2,      r3) add128(t[0], mul)
        mul64x64_128(t[1], d0, r1) mul64x64_128(mul, d4, r2) add128(t[1], mul) mul64x64_128(mul, r3, r3 * 19) add128(t[1], mul)
        mul64x64_128(t[2], d0, r2) mul64x64_128(mul, r1, r1) add128(t[2], mul) mul64x64_128(mul, d4,      r3) add128(t[2], mul)
        mul64x64_128(t[3], d0, r3) mul64x64_128(mul, d1, r2) add128(t[3], mul) mul64x64_128(mul, r4,    d419) add128(t[3], mul)
        mul64x64_128(t[4], d0, r4) mul64x64_128(mul, d1, r3) add128(t[4], mul) mul64x64_128(mul, r2,      r2) add128(t[4], mul)

        r0 = lo128(t[0]) & reduce_mask_51;
        r1 = lo128(t[1]) & reduce_mask_51; shl128(c, t[0], 13); r1 += c;
        r2 = lo128(t[2]) & reduce_mask_51; shl128(c, t[1], 13); r2 += c;
        r3 = lo128(t[3]) & reduce_mask_51; shl128(c, t[2], 13); r3 += c;
        r4 = lo128(t[4]) & reduce_mask_51; shl128(c, t[3], 13); r4 += c;
                                           shl128(c, t[4], 13); r0 += c * 19;
                       c = r0 >> 51; r0 &= reduce_mask_51;
        r1 += c     ;  c = r1 >> 51; r1 &= reduce_mask_51;
        r2 += c     ;  c = r2 >> 51; r2 &= reduce_mask_51;
        r3 += c     ;  c = r3 >> 51; r3 &= reduce_mask_51;
        r4 += c     ;  c = r4 >> 51; r4 &= reduce_mask_51;
        r0 += c * 19;
    } while(--count);

    out[0] = r0; out[1] = r1; out[2] = r2; out[3] = r3; out[4] = r4;
}

__device__ __forceinline__ static void
curve25519_square(bignum25519 out, const bignum25519 in) {
    uint128_t mul;
    uint128_t t[5];
    uint64_t r0,r1,r2,r3,r4,c;
    uint64_t d0,d1,d2,d4,d419;

    r0 = in[0]; r1 = in[1]; r2 = in[2]; r3 = in[3]; r4 = in[4];

    d0 = r0 * 2;
    d1 = r1 * 2;
    d2 = r2 * 2 * 19;
    d419 = r4 * 19;
    d4 = d419 * 2;

    mul64x64_128(t[0], r0, r0) mul64x64_128(mul, d4, r1) add128(t[0], mul) mul64x64_128(mul, d2,      r3) add128(t[0], mul)
    mul64x64_128(t[1], d0, r1) mul64x64_128(mul, d4, r2) add128(t[1], mul) mul64x64_128(mul, r3, r3 * 19) add128(t[1], mul)
    mul64x64_128(t[2], d0, r2) mul64x64_128(mul, r1, r1) add128(t[2], mul) mul64x64_128(mul, d4,      r3) add128(t[2], mul)
    mul64x64_128(t[3], d0, r3) mul64x64_128(mul, d1, r2) add128(t[3], mul) mul64x64_128(mul, r4,    d419) add128(t[3], mul)
    mul64x64_128(t[4], d0, r4) mul64x64_128(mul, d1, r3) add128(t[4], mul) mul64x64_128(mul, r2,      r2) add128(t[4], mul)

                         r0 = lo128(t[0]) & reduce_mask_51; shr128(c, t[0], 51);
    add128_64(t[1], c)   r1 = lo128(t[1]) & reduce_mask_51; shr128(c, t[1], 51);
    add128_64(t[2], c)   r2 = lo128(t[2]) & reduce_mask_51; shr128(c, t[2], 51);
    add128_64(t[3], c)   r3 = lo128(t[3]) & reduce_mask_51; shr128(c, t[3], 51);
    add128_64(t[4], c)   r4 = lo128(t[4]) & reduce_mask_51; shr128(c, t[4], 51);
    r0 +=   c * 19; c = r0 >> 51; r0 = r0 & reduce_mask_51;
    r1 +=   c;

    out[0] = r0; out[1] = r1; out[2] = r2; out[3] = r3; out[4] = r4;
}

/* Take a little-endian, 32-byte number and expand it into polynomial form.
   Byte-wise load (no aliasing assumptions) - safe on the GPU. */
__device__ __forceinline__ static void
curve25519_expand(bignum25519 out, const unsigned char *in) {
    uint64_t x0,x1,x2,x3;
    #define EXPAND_F(s)                      \
        ((((uint64_t)in[s + 0])      ) |     \
         (((uint64_t)in[s + 1]) <<  8) |     \
         (((uint64_t)in[s + 2]) << 16) |     \
         (((uint64_t)in[s + 3]) << 24) |     \
         (((uint64_t)in[s + 4]) << 32) |     \
         (((uint64_t)in[s + 5]) << 40) |     \
         (((uint64_t)in[s + 6]) << 48) |     \
         (((uint64_t)in[s + 7]) << 56))
    x0 = EXPAND_F(0);
    x1 = EXPAND_F(8);
    x2 = EXPAND_F(16);
    x3 = EXPAND_F(24);
    #undef EXPAND_F

    out[0] = x0 & reduce_mask_51; x0 = (x0 >> 51) | (x1 << 13);
    out[1] = x0 & reduce_mask_51; x1 = (x1 >> 38) | (x2 << 26);
    out[2] = x1 & reduce_mask_51; x2 = (x2 >> 25) | (x3 << 39);
    out[3] = x2 & reduce_mask_51; x3 = (x3 >> 12);
    out[4] = x3 & reduce_mask_51;
}

/* Take a fully reduced polynomial form number and contract it into a
   little-endian, 32-byte array. */
__device__ __forceinline__ static void
curve25519_contract(unsigned char *out, const bignum25519 input) {
    uint64_t t[5];
    uint64_t f, i;

    t[0] = input[0]; t[1] = input[1]; t[2] = input[2]; t[3] = input[3]; t[4] = input[4];

    #define curve25519_contract_carry() \
        t[1] += t[0] >> 51; t[0] &= reduce_mask_51; \
        t[2] += t[1] >> 51; t[1] &= reduce_mask_51; \
        t[3] += t[2] >> 51; t[2] &= reduce_mask_51; \
        t[4] += t[3] >> 51; t[3] &= reduce_mask_51;

    #define curve25519_contract_carry_full() curve25519_contract_carry() \
        t[0] += 19 * (t[4] >> 51); t[4] &= reduce_mask_51;

    #define curve25519_contract_carry_final() curve25519_contract_carry() \
        t[4] &= reduce_mask_51;

    curve25519_contract_carry_full()
    curve25519_contract_carry_full()

    t[0] += 19;
    curve25519_contract_carry_full()

    t[0] += (reduce_mask_51 + 1) - 19;
    t[1] += (reduce_mask_51 + 1) - 1;
    t[2] += (reduce_mask_51 + 1) - 1;
    t[3] += (reduce_mask_51 + 1) - 1;
    t[4] += (reduce_mask_51 + 1) - 1;

    curve25519_contract_carry_final()

    #define write51full(n,shift) \
        f = ((t[n] >> shift) | (t[n+1] << (51 - shift))); \
        for (i = 0; i < 8; i++, f >>= 8) *out++ = (unsigned char)f;
    #define write51(n) write51full(n,13*n)
    write51(0)
    write51(1)
    write51(2)
    write51(3)
    #undef write51
    #undef write51full
    #undef curve25519_contract_carry
    #undef curve25519_contract_carry_full
    #undef curve25519_contract_carry_final
}

/*
 * In:  b =   2^5 - 2^0
 * Out: b = 2^250 - 2^0
 */
__device__ static void
curve25519_pow_two5mtwo0_two250mtwo0(bignum25519 b) {
    bignum25519 t0,c;
    curve25519_square_times(t0, b, 5);
    curve25519_mul_noinline(b, t0, b);
    curve25519_square_times(t0, b, 10);
    curve25519_mul_noinline(c, t0, b);
    curve25519_square_times(t0, c, 20);
    curve25519_mul_noinline(t0, t0, c);
    curve25519_square_times(t0, t0, 10);
    curve25519_mul_noinline(b, t0, b);
    curve25519_square_times(t0, b, 50);
    curve25519_mul_noinline(c, t0, b);
    curve25519_square_times(t0, c, 100);
    curve25519_mul_noinline(t0, t0, c);
    curve25519_square_times(t0, t0, 50);
    curve25519_mul_noinline(b, t0, b);
}

/*
 * z^(p - 2) = z(2^255 - 21)
 */
__device__ static void
curve25519_recip(bignum25519 out, const bignum25519 z) {
    bignum25519 a,t0,b;
    curve25519_square_times(a, z, 1);
    curve25519_square_times(t0, a, 2);
    curve25519_mul_noinline(b, t0, z);
    curve25519_mul_noinline(a, b, a);
    curve25519_square_times(t0, a, 1);
    curve25519_mul_noinline(b, t0, b);
    curve25519_pow_two5mtwo0_two250mtwo0(b);
    curve25519_square_times(b, b, 5);
    curve25519_mul_noinline(out, b, a);
}

/* ---- Task 7 test launchers: one bignum per block, 5 limbs each ---- */

extern "C" __global__
void fe_mul_test(const uint64_t *a, const uint64_t *b, uint64_t *out, uint32_t n) {
    uint32_t i = blockIdx.x;
    if (i >= n) return;
    bignum25519 ra, rb, rc;
    for (int j = 0; j < 5; j++) { ra[j] = a[i*5+j]; rb[j] = b[i*5+j]; }
    curve25519_mul(rc, ra, rb);
    for (int j = 0; j < 5; j++) out[i*5+j] = rc[j];
}

extern "C" __global__
void fe_sq_test(const uint64_t *a, uint64_t *out, uint32_t n) {
    uint32_t i = blockIdx.x;
    if (i >= n) return;
    bignum25519 ra, rc;
    for (int j = 0; j < 5; j++) ra[j] = a[i*5+j];
    curve25519_square(rc, ra);
    for (int j = 0; j < 5; j++) out[i*5+j] = rc[j];
}

extern "C" __global__
void fe_add_test(const uint64_t *a, const uint64_t *b, uint64_t *out, uint32_t n) {
    uint32_t i = blockIdx.x;
    if (i >= n) return;
    bignum25519 ra, rb, rc;
    for (int j = 0; j < 5; j++) { ra[j] = a[i*5+j]; rb[j] = b[i*5+j]; }
    curve25519_add_reduce(rc, ra, rb);
    for (int j = 0; j < 5; j++) out[i*5+j] = rc[j];
}

extern "C" __global__
void fe_sub_test(const uint64_t *a, const uint64_t *b, uint64_t *out, uint32_t n) {
    uint32_t i = blockIdx.x;
    if (i >= n) return;
    bignum25519 ra, rb, rc;
    for (int j = 0; j < 5; j++) { ra[j] = a[i*5+j]; rb[j] = b[i*5+j]; }
    curve25519_sub_reduce(rc, ra, rb);
    for (int j = 0; j < 5; j++) out[i*5+j] = rc[j];
}

extern "C" __global__
void fe_invert_test(const uint64_t *a, uint64_t *out, uint32_t n) {
    uint32_t i = blockIdx.x;
    if (i >= n) return;
    bignum25519 ra, rc;
    for (int j = 0; j < 5; j++) ra[j] = a[i*5+j];
    curve25519_recip(rc, ra);
    for (int j = 0; j < 5; j++) out[i*5+j] = rc[j];
}
