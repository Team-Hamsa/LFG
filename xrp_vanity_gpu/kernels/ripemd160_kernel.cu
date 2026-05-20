// RIPEMD-160 device function for XRP vanity address generation
// Input: exactly 32 bytes → output: 20 bytes
// No includes, no global kernels — concatenate into your kernel.

#define RMD_ROTL(x, n) (((x) << (n)) | ((x) >> (32 - (n))))

// Boolean functions by round
#define RMD_F1(x, y, z) ((x) ^ (y) ^ (z))
#define RMD_F2(x, y, z) (((x) & (y)) | ((~(x)) & (z)))
#define RMD_F3(x, y, z) ((x) | (~(y)) ^ (z))
#define RMD_F4(x, y, z) ((x) & (z) | ((y) & (~(z))))
#define RMD_F5(x, y, z) ((x) ^ ((y) | (~(z))))

// Round constants left: 5 values, one per 16 rounds
// KL[0..4] = 0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E
// Round constants right: KR[0..4] = 0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000

__device__ void ripemd160_32(const uint8_t *in, uint8_t *out) {
    // Build the single padded 64-byte block for 32-byte input
    // bytes 0-31: input
    // byte 32: 0x80
    // bytes 33-55: 0x00
    // bytes 56-59: 0x00 0x01 0x00 0x00 (little-endian bit length = 256)
    // bytes 60-63: 0x00 0x00 0x00 0x00

    uint32_t W[16];
    // Load input as 8 little-endian 32-bit words
    for (int i = 0; i < 8; i++) {
        W[i] = ((uint32_t)in[i*4+0])
             | ((uint32_t)in[i*4+1] << 8)
             | ((uint32_t)in[i*4+2] << 16)
             | ((uint32_t)in[i*4+3] << 24);
    }
    // Padding: byte 32 = 0x80, rest zero except length
    // W[8]: byte32=0x80, bytes33-35=0x00 → 0x00000080
    W[8]  = 0x00000080u;
    W[9]  = 0u;
    W[10] = 0u;
    W[11] = 0u;
    W[12] = 0u;
    W[13] = 0u;
    // W[14]: bit length low word = 256 = 0x00000100 (little-endian uint64 lo)
    W[14] = 256u;
    // W[15]: bit length high word = 0
    W[15] = 0u;

    // Initial state
    uint32_t h0 = 0x67452301u;
    uint32_t h1 = 0xEFCDAB89u;
    uint32_t h2 = 0x98BADCFEu;
    uint32_t h3 = 0x10325476u;
    uint32_t h4 = 0xC3D2E1F0u;

    uint32_t aL = h0, bL = h1, cL = h2, dL = h3, eL = h4;
    uint32_t aR = h0, bR = h1, cR = h2, dR = h3, eR = h4;

    // Message word indices left
    const int ML[80] = {
        0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,
        7,4,13,1,10,6,15,3,12,0,9,5,2,14,11,8,
        3,10,14,4,9,15,8,1,2,7,0,6,13,11,5,12,
        1,9,11,10,0,8,12,4,13,3,7,15,14,5,6,2,
        4,0,5,9,7,12,2,10,14,1,3,8,11,6,15,13
    };

    // Message word indices right
    const int MR[80] = {
        5,14,7,0,9,2,11,4,13,6,15,8,1,10,3,12,
        6,11,3,7,0,13,5,10,14,15,8,12,4,9,1,2,
        15,5,1,3,7,14,6,9,11,8,12,2,10,0,4,13,
        8,6,4,1,3,11,15,0,5,12,2,13,9,7,10,14,
        12,15,10,4,1,5,8,7,6,2,13,14,0,3,9,11
    };

    // Rotation amounts left
    const int SL[80] = {
        11,14,15,12,5,8,7,9,11,13,14,15,6,7,9,8,
        7,6,8,13,11,9,7,15,7,12,15,9,11,7,13,12,
        11,13,6,7,14,9,13,15,14,8,13,6,5,12,7,5,
        11,12,14,15,14,15,9,8,9,14,5,6,8,6,5,12,
        9,15,5,11,6,8,13,12,5,12,13,14,11,8,5,6
    };

    // Rotation amounts right
    const int SR[80] = {
        8,9,9,11,13,15,15,5,7,7,8,11,14,14,12,6,
        9,13,15,7,12,8,9,11,7,7,12,7,6,15,13,11,
        9,7,15,11,8,6,6,14,12,13,5,14,13,13,7,5,
        15,5,8,11,14,14,6,14,6,9,12,9,12,5,15,8,
        8,5,12,9,12,5,14,6,8,13,6,5,15,13,11,11
    };

    for (int i = 0; i < 80; i++) {
        uint32_t fL, fR;
        uint32_t kL, kR;

        int round = i / 16;

        // Left boolean function
        switch (round) {
            case 0: fL = RMD_F1(bL, cL, dL); kL = 0x00000000u; break;
            case 1: fL = RMD_F2(bL, cL, dL); kL = 0x5A827999u; break;
            case 2: fL = RMD_F3(bL, cL, dL); kL = 0x6ED9EBA1u; break;
            case 3: fL = RMD_F4(bL, cL, dL); kL = 0x8F1BBCDCu; break;
            default:fL = RMD_F5(bL, cL, dL); kL = 0xA953FD4Eu; break;
        }

        // Right boolean function (reverse order: 0→4, 1→3, 2→2, 3→1, 4→0)
        switch (round) {
            case 0: fR = RMD_F5(bR, cR, dR); kR = 0x50A28BE6u; break;
            case 1: fR = RMD_F4(bR, cR, dR); kR = 0x5C4DD124u; break;
            case 2: fR = RMD_F3(bR, cR, dR); kR = 0x6D703EF3u; break;
            case 3: fR = RMD_F2(bR, cR, dR); kR = 0x7A6D76E9u; break;
            default:fR = RMD_F1(bR, cR, dR); kR = 0x00000000u; break;
        }

        uint32_t tL = RMD_ROTL(aL + fL + W[ML[i]] + kL, SL[i]) + eL;
        aL = eL; eL = dL; dL = RMD_ROTL(cL, 10); cL = bL; bL = tL;

        uint32_t tR = RMD_ROTL(aR + fR + W[MR[i]] + kR, SR[i]) + eR;
        aR = eR; eR = dR; dR = RMD_ROTL(cR, 10); cR = bR; bR = tR;
    }

    // Final combine
    uint32_t t = h1 + cL + dR;
    h1 = h2 + dL + eR;
    h2 = h3 + eL + aR;
    h3 = h4 + aL + bR;
    h4 = h0 + bL + cR;
    h0 = t;

    // Output each hi as 4 bytes little-endian
    #define STORE32LE(val, ptr) \
        (ptr)[0] = (uint8_t)((val));       (ptr)[1] = (uint8_t)((val) >> 8); \
        (ptr)[2] = (uint8_t)((val) >> 16); (ptr)[3] = (uint8_t)((val) >> 24);

    STORE32LE(h0, out +  0)
    STORE32LE(h1, out +  4)
    STORE32LE(h2, out +  8)
    STORE32LE(h3, out + 12)
    STORE32LE(h4, out + 16)
    #undef STORE32LE
}
