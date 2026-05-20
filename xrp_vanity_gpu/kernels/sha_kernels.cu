// SHA-512 and SHA-256 device functions for XRP vanity address generation
// Fixed-length inputs: SHA-512(16 bytes), SHA-256(33/21/32 bytes)
// No includes, no global kernels - include/concatenate into your kernel.

// ??? SHA-512 ????????????????????????????????????????????????????????????????

__device__ static const uint64_t SHA512_K[80] = {
    0x428a2f98d728ae22ULL, 0x7137449123ef65cdULL, 0xb5c0fbcfec4d3b2fULL, 0xe9b5dba58189dbbcULL,
    0x3956c25bf348b538ULL, 0x59f111f1b605d019ULL, 0x923f82a4af194f9bULL, 0xab1c5ed5da6d8118ULL,
    0xd807aa98a3030242ULL, 0x12835b0145706fbeULL, 0x243185be4ee4b28cULL, 0x550c7dc3d5ffb4e2ULL,
    0x72be5d74f27b896fULL, 0x80deb1fe3b1696b1ULL, 0x9bdc06a725c71235ULL, 0xc19bf174cf692694ULL,
    0xe49b69c19ef14ad2ULL, 0xefbe4786384f25e3ULL, 0x0fc19dc68b8cd5b5ULL, 0x240ca1cc77ac9c65ULL,
    0x2de92c6f592b0275ULL, 0x4a7484aa6ea6e483ULL, 0x5cb0a9dcbd41fbd4ULL, 0x76f988da831153b5ULL,
    0x983e5152ee66dfabULL, 0xa831c66d2db43210ULL, 0xb00327c898fb213fULL, 0xbf597fc7beef0ee4ULL,
    0xc6e00bf33da88fc2ULL, 0xd5a79147930aa725ULL, 0x06ca6351e003826fULL, 0x142929670a0e6e70ULL,
    0x27b70a8546d22ffcULL, 0x2e1b21385c26c926ULL, 0x4d2c6dfc5ac42aedULL, 0x53380d139d95b3dfULL,
    0x650a73548baf63deULL, 0x766a0abb3c77b2a8ULL, 0x81c2c92e47edaee6ULL, 0x92722c851482353bULL,
    0xa2bfe8a14cf10364ULL, 0xa81a664bbc423001ULL, 0xc24b8b70d0f89791ULL, 0xc76c51a30654be30ULL,
    0xd192e819d6ef5218ULL, 0xd69906245565a910ULL, 0xf40e35855771202aULL, 0x106aa07032bbd1b8ULL,
    0x19a4c116b8d2d0c8ULL, 0x1e376c085141ab53ULL, 0x2748774cdf8eeb99ULL, 0x34b0bcb5e19b48a8ULL,
    0x391c0cb3c5c95a63ULL, 0x4ed8aa4ae3418acbULL, 0x5b9cca4f7763e373ULL, 0x682e6ff3d6b2b8a3ULL,
    0x748f82ee5defb2fcULL, 0x78a5636f43172f60ULL, 0x84c87814a1f0ab72ULL, 0x8cc702081a6439ecULL,
    0x90befffa23631e28ULL, 0xa4506cebde82bde9ULL, 0xbef9a3f7b2c67915ULL, 0xc67178f2e372532bULL,
    0xca273eceea26619cULL, 0xd186b8c721c0c207ULL, 0xeada7dd6cde0eb1eULL, 0xf57d4f7fee6ed178ULL,
    0x06f067aa72176fbaULL, 0x0a637dc5a2c898a6ULL, 0x113f9804bef90daeULL, 0x1b710b35131c471bULL,
    0x28db77f523047d84ULL, 0x32caab7b40c72493ULL, 0x3c9ebe0a15c9bebcULL, 0x431d67c49c100d4cULL,
    0x4cc5d4becb3e42b6ULL, 0x597f299cfc657e2aULL, 0x5fcb6fab3ad6faecULL, 0x6c44198c4a475817ULL,
};

#define SHA512_ROTR(x, n) (((x) >> (n)) | ((x) << (64 - (n))))
#define SHA512_CH(e, f, g)  (((e) & (f)) ^ (~(e) & (g)))
#define SHA512_MAJ(a, b, c) (((a) & (b)) ^ ((a) & (c)) ^ ((b) & (c)))
#define SHA512_SIG0(x) (SHA512_ROTR(x,28) ^ SHA512_ROTR(x,34) ^ SHA512_ROTR(x,39))
#define SHA512_SIG1(x) (SHA512_ROTR(x,14) ^ SHA512_ROTR(x,18) ^ SHA512_ROTR(x,41))
#define SHA512_GAM0(x) (SHA512_ROTR(x, 1) ^ SHA512_ROTR(x, 8) ^ ((x) >> 7))
#define SHA512_GAM1(x) (SHA512_ROTR(x,19) ^ SHA512_ROTR(x,61) ^ ((x) >> 6))

// SHA-512 on exactly 16 bytes of input ? 64 bytes output (single 128-byte block)
__device__ void sha512_16(const uint8_t *in, uint8_t *out) {
    // Initial hash values (first 64 bits of fractional parts of sqrt of first 8 primes)
    uint64_t h0 = 0x6a09e667f3bcc908ULL;
    uint64_t h1 = 0xbb67ae8584caa73bULL;
    uint64_t h2 = 0x3c6ef372fe94f82bULL;
    uint64_t h3 = 0xa54ff53a5f1d36f1ULL;
    uint64_t h4 = 0x510e527fade682d1ULL;
    uint64_t h5 = 0x9b05688c2b3e6c1fULL;
    uint64_t h6 = 0x1f83d9abfb41bd6bULL;
    uint64_t h7 = 0x5be0cd19137e2179ULL;

    // Build the single 128-byte (16 word) message block
    // Padding for 16-byte input:
    //   word[0..1] = input bytes as big-endian uint64
    //   word[2]    = 0x8000000000000000 (0x80 pad byte in MSB)
    //   word[3..14]= 0
    //   word[15]   = 128 (bit-length = 16*8)
    uint64_t W[80];

    // Load input as two big-endian 64-bit words
    W[0] = ((uint64_t)in[ 0] << 56) | ((uint64_t)in[ 1] << 48) |
           ((uint64_t)in[ 2] << 40) | ((uint64_t)in[ 3] << 32) |
           ((uint64_t)in[ 4] << 24) | ((uint64_t)in[ 5] << 16) |
           ((uint64_t)in[ 6] <<  8) |  (uint64_t)in[ 7];
    W[1] = ((uint64_t)in[ 8] << 56) | ((uint64_t)in[ 9] << 48) |
           ((uint64_t)in[10] << 40) | ((uint64_t)in[11] << 32) |
           ((uint64_t)in[12] << 24) | ((uint64_t)in[13] << 16) |
           ((uint64_t)in[14] <<  8) |  (uint64_t)in[15];
    W[2]  = 0x8000000000000000ULL;
    W[3]  = 0ULL; W[4]  = 0ULL; W[5]  = 0ULL; W[6]  = 0ULL;
    W[7]  = 0ULL; W[8]  = 0ULL; W[9]  = 0ULL; W[10] = 0ULL;
    W[11] = 0ULL; W[12] = 0ULL; W[13] = 0ULL; W[14] = 0ULL;
    W[15] = 128ULL; // bit length

    // Expand message schedule
    for (int i = 16; i < 80; i++) {
        W[i] = SHA512_GAM1(W[i-2]) + W[i-7] + SHA512_GAM0(W[i-15]) + W[i-16];
    }

    // Compression
    uint64_t a = h0, b = h1, c = h2, d = h3;
    uint64_t e = h4, f = h5, g = h6, h = h7;

    for (int i = 0; i < 80; i++) {
        uint64_t T1 = h + SHA512_SIG1(e) + SHA512_CH(e, f, g) + SHA512_K[i] + W[i];
        uint64_t T2 = SHA512_SIG0(a) + SHA512_MAJ(a, b, c);
        h = g; g = f; f = e; e = d + T1;
        d = c; c = b; b = a; a = T1 + T2;
    }

    h0 += a; h1 += b; h2 += c; h3 += d;
    h4 += e; h5 += f; h6 += g; h7 += h;

    // Store result as big-endian bytes
    #define STORE64BE(val, ptr) \
        (ptr)[0] = (uint8_t)((val) >> 56); (ptr)[1] = (uint8_t)((val) >> 48); \
        (ptr)[2] = (uint8_t)((val) >> 40); (ptr)[3] = (uint8_t)((val) >> 32); \
        (ptr)[4] = (uint8_t)((val) >> 24); (ptr)[5] = (uint8_t)((val) >> 16); \
        (ptr)[6] = (uint8_t)((val) >>  8); (ptr)[7] = (uint8_t)((val));

    STORE64BE(h0, out +  0)
    STORE64BE(h1, out +  8)
    STORE64BE(h2, out + 16)
    STORE64BE(h3, out + 24)
    STORE64BE(h4, out + 32)
    STORE64BE(h5, out + 40)
    STORE64BE(h6, out + 48)
    STORE64BE(h7, out + 56)
    #undef STORE64BE
}

// ??? SHA-256 ????????????????????????????????????????????????????????????????

__device__ static const uint32_t SHA256_K[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u,
    0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
    0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u,
    0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
    0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu,
    0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u,
    0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
    0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u,
    0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u,
    0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x27487740u, 0x34b0bcb5u,
    0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u,
};

#define SHA256_ROTR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))
#define SHA256_CH(e, f, g)  (((e) & (f)) ^ (~(e) & (g)))
#define SHA256_MAJ(a, b, c) (((a) & (b)) ^ ((a) & (c)) ^ ((b) & (c)))
#define SHA256_SIG0(x) (SHA256_ROTR(x, 2) ^ SHA256_ROTR(x,13) ^ SHA256_ROTR(x,22))
#define SHA256_SIG1(x) (SHA256_ROTR(x, 6) ^ SHA256_ROTR(x,11) ^ SHA256_ROTR(x,25))
#define SHA256_GAM0(x) (SHA256_ROTR(x, 7) ^ SHA256_ROTR(x,18) ^ ((x) >>  3))
#define SHA256_GAM1(x) (SHA256_ROTR(x,17) ^ SHA256_ROTR(x,19) ^ ((x) >> 10))

// Build W[0..15] from a padded 64-byte block then expand W[16..63]
// and run compression. Adds into h0..h7 (caller must initialize them first).
__device__ static void sha256_compress(
        const uint8_t block[64],
        uint32_t *h0, uint32_t *h1, uint32_t *h2, uint32_t *h3,
        uint32_t *h4, uint32_t *h5, uint32_t *h6, uint32_t *h7)
{
    uint32_t W[64];
    for (int i = 0; i < 16; i++) {
        W[i] = ((uint32_t)block[i*4+0] << 24) |
               ((uint32_t)block[i*4+1] << 16) |
               ((uint32_t)block[i*4+2] <<  8) |
                (uint32_t)block[i*4+3];
    }
    for (int i = 16; i < 64; i++) {
        W[i] = SHA256_GAM1(W[i-2]) + W[i-7] + SHA256_GAM0(W[i-15]) + W[i-16];
    }

    uint32_t a = *h0, b = *h1, c = *h2, d = *h3;
    uint32_t e = *h4, f = *h5, g = *h6, h = *h7;

    for (int i = 0; i < 64; i++) {
        uint32_t T1 = h + SHA256_SIG1(e) + SHA256_CH(e,f,g) + SHA256_K[i] + W[i];
        uint32_t T2 = SHA256_SIG0(a) + SHA256_MAJ(a,b,c);
        h = g; g = f; f = e; e = d + T1;
        d = c; c = b; b = a; a = T1 + T2;
    }

    *h0 += a; *h1 += b; *h2 += c; *h3 += d;
    *h4 += e; *h5 += f; *h6 += g; *h7 += h;
}

__device__ static void sha256_store_result(
        uint32_t h0, uint32_t h1, uint32_t h2, uint32_t h3,
        uint32_t h4, uint32_t h5, uint32_t h6, uint32_t h7,
        uint8_t *out)
{
    #define STORE32BE(val, ptr) \
        (ptr)[0] = (uint8_t)((val) >> 24); (ptr)[1] = (uint8_t)((val) >> 16); \
        (ptr)[2] = (uint8_t)((val) >>  8); (ptr)[3] = (uint8_t)((val));
    STORE32BE(h0, out +  0) STORE32BE(h1, out +  4)
    STORE32BE(h2, out +  8) STORE32BE(h3, out + 12)
    STORE32BE(h4, out + 16) STORE32BE(h5, out + 20)
    STORE32BE(h6, out + 24) STORE32BE(h7, out + 28)
    #undef STORE32BE
}

// SHA-256 on exactly 33 bytes of input ? 32 bytes output (single 64-byte block)
// Padding: bytes[0..32]=input, byte[33]=0x80, bytes[34..59]=0, bytes[60..63]=0x00000108
__device__ void sha256_33(const uint8_t *in, uint8_t *out) {
    uint8_t block[64];
    for (int i = 0; i < 33; i++) block[i] = in[i];
    block[33] = 0x80u;
    for (int i = 34; i < 60; i++) block[i] = 0u;
    block[60] = 0x00u; block[61] = 0x00u; block[62] = 0x01u; block[63] = 0x08u;

    uint32_t h0 = 0x6a09e667u, h1 = 0xbb67ae85u, h2 = 0x3c6ef372u, h3 = 0xa54ff53au;
    uint32_t h4 = 0x510e527fu, h5 = 0x9b05688cu, h6 = 0x1f83d9abu, h7 = 0x5be0cd19u;
    sha256_compress(block, &h0, &h1, &h2, &h3, &h4, &h5, &h6, &h7);
    sha256_store_result(h0, h1, h2, h3, h4, h5, h6, h7, out);
}

// SHA-256 on exactly 21 bytes of input ? 32 bytes output (single 64-byte block)
// Padding: bytes[0..20]=input, byte[21]=0x80, bytes[22..59]=0, bytes[60..63]=0x000000A8
__device__ void sha256_21(const uint8_t *in, uint8_t *out) {
    uint8_t block[64];
    for (int i = 0; i < 21; i++) block[i] = in[i];
    block[21] = 0x80u;
    for (int i = 22; i < 60; i++) block[i] = 0u;
    block[60] = 0x00u; block[61] = 0x00u; block[62] = 0x00u; block[63] = 0xA8u;

    uint32_t h0 = 0x6a09e667u, h1 = 0xbb67ae85u, h2 = 0x3c6ef372u, h3 = 0xa54ff53au;
    uint32_t h4 = 0x510e527fu, h5 = 0x9b05688cu, h6 = 0x1f83d9abu, h7 = 0x5be0cd19u;
    sha256_compress(block, &h0, &h1, &h2, &h3, &h4, &h5, &h6, &h7);
    sha256_store_result(h0, h1, h2, h3, h4, h5, h6, h7, out);
}

// SHA-256 on exactly 32 bytes of input ? 32 bytes output (single 64-byte block)
// Padding: bytes[0..31]=input, byte[32]=0x80, bytes[33..59]=0, bytes[60..63]=0x00000100
__device__ void sha256_32(const uint8_t *in, uint8_t *out) {
    uint8_t block[64];
    for (int i = 0; i < 32; i++) block[i] = in[i];
    block[32] = 0x80u;
    for (int i = 33; i < 60; i++) block[i] = 0u;
    block[60] = 0x00u; block[61] = 0x00u; block[62] = 0x01u; block[63] = 0x00u;

    uint32_t h0 = 0x6a09e667u, h1 = 0xbb67ae85u, h2 = 0x3c6ef372u, h3 = 0xa54ff53au;
    uint32_t h4 = 0x510e527fu, h5 = 0x9b05688cu, h6 = 0x1f83d9abu, h7 = 0x5be0cd19u;
    sha256_compress(block, &h0, &h1, &h2, &h3, &h4, &h5, &h6, &h7);
    sha256_store_result(h0, h1, h2, h3, h4, h5, h6, h7, out);
}
