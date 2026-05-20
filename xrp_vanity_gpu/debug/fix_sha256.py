"""Rewrite sha_kernels.cu with inline K constants (avoids static const array issues in NVRTC)"""
import hashlib, cupy as cp

# The issue: __device__ static const arrays in NVRTC can misbehave.
# Fix: put K[] inside the function as a local const array, or use __constant__.
# Simplest fix for NVRTC: declare K inline inside sha256_compress.

SHA_KERNELS = r"""
// ===== SHA-512 =====
#define SHA512_ROTR(x,n) (((x)>>(n))|((x)<<(64-(n))))
#define SHA512_CH(e,f,g)  (((e)&(f))^(~(e)&(g)))
#define SHA512_MAJ(a,b,c) (((a)&(b))^((a)&(c))^((b)&(c)))
#define SHA512_SIG0(x) (SHA512_ROTR(x,28)^SHA512_ROTR(x,34)^SHA512_ROTR(x,39))
#define SHA512_SIG1(x) (SHA512_ROTR(x,14)^SHA512_ROTR(x,18)^SHA512_ROTR(x,41))
#define SHA512_GAM0(x) (SHA512_ROTR(x,1)^SHA512_ROTR(x,8)^((x)>>7))
#define SHA512_GAM1(x) (SHA512_ROTR(x,19)^SHA512_ROTR(x,61)^((x)>>6))

__device__ void sha512_16(const uint8_t *in, uint8_t *out) {
    const uint64_t K[80] = {
        0x428a2f98d728ae22ULL,0x7137449123ef65cdULL,0xb5c0fbcfec4d3b2fULL,0xe9b5dba58189dbbcULL,
        0x3956c25bf348b538ULL,0x59f111f1b605d019ULL,0x923f82a4af194f9bULL,0xab1c5ed5da6d8118ULL,
        0xd807aa98a3030242ULL,0x12835b0145706fbeULL,0x243185be4ee4b28cULL,0x550c7dc3d5ffb4e2ULL,
        0x72be5d74f27b896fULL,0x80deb1fe3b1696b1ULL,0x9bdc06a725c71235ULL,0xc19bf174cf692694ULL,
        0xe49b69c19ef14ad2ULL,0xefbe4786384f25e3ULL,0x0fc19dc68b8cd5b5ULL,0x240ca1cc77ac9c65ULL,
        0x2de92c6f592b0275ULL,0x4a7484aa6ea6e483ULL,0x5cb0a9dcbd41fbd4ULL,0x76f988da831153b5ULL,
        0x983e5152ee66dfabULL,0xa831c66d2db43210ULL,0xb00327c898fb213fULL,0xbf597fc7beef0ee4ULL,
        0xc6e00bf33da88fc2ULL,0xd5a79147930aa725ULL,0x06ca6351e003826fULL,0x142929670a0e6e70ULL,
        0x27b70a8546d22ffcULL,0x2e1b21385c26c926ULL,0x4d2c6dfc5ac42aedULL,0x53380d139d95b3dfULL,
        0x650a73548baf63deULL,0x766a0abb3c77b2a8ULL,0x81c2c92e47edaee6ULL,0x92722c851482353bULL,
        0xa2bfe8a14cf10364ULL,0xa81a664bbc423001ULL,0xc24b8b70d0f89791ULL,0xc76c51a30654be30ULL,
        0xd192e819d6ef5218ULL,0xd69906245565a910ULL,0xf40e35855771202aULL,0x106aa07032bbd1b8ULL,
        0x19a4c116b8d2d0c8ULL,0x1e376c085141ab53ULL,0x2748774cdf8eeb99ULL,0x34b0bcb5e19b48a8ULL,
        0x391c0cb3c5c95a63ULL,0x4ed8aa4ae3418acbULL,0x5b9cca4f7763e373ULL,0x682e6ff3d6b2b8a3ULL,
        0x748f82ee5defb2fcULL,0x78a5636f43172f60ULL,0x84c87814a1f0ab72ULL,0x8cc702081a6439ecULL,
        0x90befffa23631e28ULL,0xa4506cebde82bde9ULL,0xbef9a3f7b2c67915ULL,0xc67178f2e372532bULL,
        0xca273eceea26619cULL,0xd186b8c721c0c207ULL,0xeada7dd6cde0eb1eULL,0xf57d4f7fee6ed178ULL,
        0x06f067aa72176fbaULL,0x0a637dc5a2c898a6ULL,0x113f9804bef90daeULL,0x1b710b35131c471bULL,
        0x28db77f523047d84ULL,0x32caab7b40c72493ULL,0x3c9ebe0a15c9bebcULL,0x431d67c49c100d4cULL,
        0x4cc5d4becb3e42b6ULL,0x597f299cfc657e2aULL,0x5fcb6fab3ad6faecULL,0x6c44198c4a475817ULL
    };
    uint64_t h0=0x6a09e667f3bcc908ULL,h1=0xbb67ae8584caa73bULL;
    uint64_t h2=0x3c6ef372fe94f82bULL,h3=0xa54ff53a5f1d36f1ULL;
    uint64_t h4=0x510e527fade682d1ULL,h5=0x9b05688c2b3e6c1fULL;
    uint64_t h6=0x1f83d9abfb41bd6bULL,h7=0x5be0cd19137e2179ULL;
    uint64_t W[80];
    W[0]=((uint64_t)in[0]<<56)|((uint64_t)in[1]<<48)|((uint64_t)in[2]<<40)|((uint64_t)in[3]<<32)|
         ((uint64_t)in[4]<<24)|((uint64_t)in[5]<<16)|((uint64_t)in[6]<<8)|(uint64_t)in[7];
    W[1]=((uint64_t)in[8]<<56)|((uint64_t)in[9]<<48)|((uint64_t)in[10]<<40)|((uint64_t)in[11]<<32)|
         ((uint64_t)in[12]<<24)|((uint64_t)in[13]<<16)|((uint64_t)in[14]<<8)|(uint64_t)in[15];
    W[2]=0x8000000000000000ULL;
    for(int i=3;i<15;i++) W[i]=0ULL;
    W[15]=128ULL;
    for(int i=16;i<80;i++) W[i]=SHA512_GAM1(W[i-2])+W[i-7]+SHA512_GAM0(W[i-15])+W[i-16];
    uint64_t a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,h=h7;
    for(int i=0;i<80;i++){
        uint64_t T1=h+SHA512_SIG1(e)+SHA512_CH(e,f,g)+K[i]+W[i];
        uint64_t T2=SHA512_SIG0(a)+SHA512_MAJ(a,b,c);
        h=g;g=f;f=e;e=d+T1;d=c;c=b;b=a;a=T1+T2;
    }
    h0+=a;h1+=b;h2+=c;h3+=d;h4+=e;h5+=f;h6+=g;h7+=h;
    #define S64(v,p) (p)[0]=(uint8_t)((v)>>56);(p)[1]=(uint8_t)((v)>>48);(p)[2]=(uint8_t)((v)>>40);(p)[3]=(uint8_t)((v)>>32);(p)[4]=(uint8_t)((v)>>24);(p)[5]=(uint8_t)((v)>>16);(p)[6]=(uint8_t)((v)>>8);(p)[7]=(uint8_t)(v);
    S64(h0,out+0) S64(h1,out+8) S64(h2,out+16) S64(h3,out+24)
    S64(h4,out+32) S64(h5,out+40) S64(h6,out+48) S64(h7,out+56)
    #undef S64
}

// ===== SHA-256 helpers =====
#define SHA256_ROTR(x,n) (((x)>>(n))|((x)<<(32-(n))))
#define SHA256_CH(e,f,g)  (((e)&(f))^(~(e)&(g)))
#define SHA256_MAJ(a,b,c) (((a)&(b))^((a)&(c))^((b)&(c)))
#define SHA256_SIG0(x) (SHA256_ROTR(x,2)^SHA256_ROTR(x,13)^SHA256_ROTR(x,22))
#define SHA256_SIG1(x) (SHA256_ROTR(x,6)^SHA256_ROTR(x,11)^SHA256_ROTR(x,25))
#define SHA256_GAM0(x) (SHA256_ROTR(x,7)^SHA256_ROTR(x,18)^((x)>>3))
#define SHA256_GAM1(x) (SHA256_ROTR(x,17)^SHA256_ROTR(x,19)^((x)>>10))
#define SHA256_INIT uint32_t h0=0x6a09e667u,h1=0xbb67ae85u,h2=0x3c6ef372u,h3=0xa54ff53au,h4=0x510e527fu,h5=0x9b05688cu,h6=0x1f83d9abu,h7=0x5be0cd19u;
#define SHA256_COMPRESS(W) { \
    const uint32_t K[64]={0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,0x19a4c116u,0x1e376c08u,0x27487740u,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u}; \
    for(int _i=16;_i<64;_i++) W[_i]=SHA256_GAM1(W[_i-2])+W[_i-7]+SHA256_GAM0(W[_i-15])+W[_i-16]; \
    uint32_t _a=h0,_b=h1,_c=h2,_d=h3,_e=h4,_f=h5,_g=h6,_h=h7; \
    for(int _i=0;_i<64;_i++){uint32_t _T1=_h+SHA256_SIG1(_e)+SHA256_CH(_e,_f,_g)+K[_i]+W[_i];uint32_t _T2=SHA256_SIG0(_a)+SHA256_MAJ(_a,_b,_c);_h=_g;_g=_f;_f=_e;_e=_d+_T1;_d=_c;_c=_b;_b=_a;_a=_T1+_T2;} \
    h0+=_a;h1+=_b;h2+=_c;h3+=_d;h4+=_e;h5+=_f;h6+=_g;h7+=_h; \
}
#define SHA256_STORE(out) {out[0]=(uint8_t)(h0>>24);out[1]=(uint8_t)(h0>>16);out[2]=(uint8_t)(h0>>8);out[3]=(uint8_t)h0;out[4]=(uint8_t)(h1>>24);out[5]=(uint8_t)(h1>>16);out[6]=(uint8_t)(h1>>8);out[7]=(uint8_t)h1;out[8]=(uint8_t)(h2>>24);out[9]=(uint8_t)(h2>>16);out[10]=(uint8_t)(h2>>8);out[11]=(uint8_t)h2;out[12]=(uint8_t)(h3>>24);out[13]=(uint8_t)(h3>>16);out[14]=(uint8_t)(h3>>8);out[15]=(uint8_t)h3;out[16]=(uint8_t)(h4>>24);out[17]=(uint8_t)(h4>>16);out[18]=(uint8_t)(h4>>8);out[19]=(uint8_t)h4;out[20]=(uint8_t)(h5>>24);out[21]=(uint8_t)(h5>>16);out[22]=(uint8_t)(h5>>8);out[23]=(uint8_t)h5;out[24]=(uint8_t)(h6>>24);out[25]=(uint8_t)(h6>>16);out[26]=(uint8_t)(h6>>8);out[27]=(uint8_t)h6;out[28]=(uint8_t)(h7>>24);out[29]=(uint8_t)(h7>>16);out[30]=(uint8_t)(h7>>8);out[31]=(uint8_t)h7;}

// SHA-256 of exactly 19 bytes -> 32 bytes  (seed checksum: prefix+entropy = 19 bytes)
// Padding: byte[19]=0x80, bytes[20..59]=0, bytes[60..63]=0x00 0x00 0x00 0x98 (152 bits)
__device__ void sha256_19(const uint8_t *in, uint8_t *out) {
    uint32_t W[64];
    W[0]=((uint32_t)in[0]<<24)|((uint32_t)in[1]<<16)|((uint32_t)in[2]<<8)|(uint32_t)in[3];
    W[1]=((uint32_t)in[4]<<24)|((uint32_t)in[5]<<16)|((uint32_t)in[6]<<8)|(uint32_t)in[7];
    W[2]=((uint32_t)in[8]<<24)|((uint32_t)in[9]<<16)|((uint32_t)in[10]<<8)|(uint32_t)in[11];
    W[3]=((uint32_t)in[12]<<24)|((uint32_t)in[13]<<16)|((uint32_t)in[14]<<8)|(uint32_t)in[15];
    W[4]=((uint32_t)in[16]<<24)|((uint32_t)in[17]<<16)|((uint32_t)in[18]<<8)|0x80u;
    for(int i=5;i<15;i++) W[i]=0u;
    W[15]=152u;
    SHA256_INIT
    SHA256_COMPRESS(W)
    SHA256_STORE(out)
}

// SHA-256 of exactly 21 bytes -> 32 bytes
// Padding: byte[21]=0x80, bytes[22..59]=0, bytes[60..63]=0x000000A8 (168 bits)
__device__ void sha256_21(const uint8_t *in, uint8_t *out) {
    uint32_t W[64];
    W[0]=((uint32_t)in[0]<<24)|((uint32_t)in[1]<<16)|((uint32_t)in[2]<<8)|(uint32_t)in[3];
    W[1]=((uint32_t)in[4]<<24)|((uint32_t)in[5]<<16)|((uint32_t)in[6]<<8)|(uint32_t)in[7];
    W[2]=((uint32_t)in[8]<<24)|((uint32_t)in[9]<<16)|((uint32_t)in[10]<<8)|(uint32_t)in[11];
    W[3]=((uint32_t)in[12]<<24)|((uint32_t)in[13]<<16)|((uint32_t)in[14]<<8)|(uint32_t)in[15];
    W[4]=((uint32_t)in[16]<<24)|((uint32_t)in[17]<<16)|((uint32_t)in[18]<<8)|(uint32_t)in[19];
    W[5]=((uint32_t)in[20]<<24)|0x00800000u;
    for(int i=6;i<15;i++) W[i]=0u;
    W[15]=168u;
    SHA256_INIT
    SHA256_COMPRESS(W)
    SHA256_STORE(out)
}

// SHA-256 of exactly 32 bytes -> 32 bytes
// Padding: byte[32]=0x80, bytes[33..59]=0, bytes[60..63]=0x00000100 (256 bits)
__device__ void sha256_32(const uint8_t *in, uint8_t *out) {
    uint32_t W[64];
    for(int i=0;i<8;i++) W[i]=((uint32_t)in[i*4]<<24)|((uint32_t)in[i*4+1]<<16)|((uint32_t)in[i*4+2]<<8)|(uint32_t)in[i*4+3];
    W[8]=0x80000000u;
    for(int i=9;i<15;i++) W[i]=0u;
    W[15]=256u;
    SHA256_INIT
    SHA256_COMPRESS(W)
    SHA256_STORE(out)
}

// SHA-256 of exactly 33 bytes -> 32 bytes
// Padding: byte[33]=0x80, bytes[34..59]=0, bytes[60..63]=0x00000108 (264 bits)
__device__ void sha256_33(const uint8_t *in, uint8_t *out) {
    uint32_t W[64];
    for(int i=0;i<8;i++) W[i]=((uint32_t)in[i*4]<<24)|((uint32_t)in[i*4+1]<<16)|((uint32_t)in[i*4+2]<<8)|(uint32_t)in[i*4+3];
    W[8]=((uint32_t)in[32]<<24)|0x00800000u;
    for(int i=9;i<15;i++) W[i]=0u;
    W[15]=264u;
    SHA256_INIT
    SHA256_COMPRESS(W)
    SHA256_STORE(out)
}
"""

PREAMBLE = """
typedef unsigned char      uint8_t;
typedef unsigned int       uint32_t;
typedef unsigned long long uint64_t;
typedef signed int         int32_t;
typedef signed long long   int64_t;
"""

b58_code = open('/home/hamsa/xrp_vanity_parts/base58_kernel.cu').read()
src = PREAMBLE + SHA_KERNELS + b58_code + r"""
extern "C" __global__ void t_sha512(const uint8_t *i, uint8_t *o) { sha512_16(i, o); }
extern "C" __global__ void t_sha256_32(const uint8_t *i, uint8_t *o) { sha256_32(i, o); }
extern "C" __global__ void t_sha256_33(const uint8_t *i, uint8_t *o) { sha256_33(i, o); }
extern "C" __global__ void t_sha256_21(const uint8_t *i, uint8_t *o) { sha256_21(i, o); }
extern "C" __global__ void t_sha256_19(const uint8_t *i, uint8_t *o) { sha256_19(i, o); }
"""

print("Compiling...", flush=True)
def run(k, inp, olen):
    i = cp.frombuffer(inp, dtype=cp.uint8)
    o = cp.zeros(olen, dtype=cp.uint8)
    k((1,),(1,),(i, o))
    return o.get().tobytes()

import hashlib
def K(name): return cp.RawKernel(src, name)

ok = True
for name, func, data in [
    ('SHA512_16z', 't_sha512', bytes(16)),
    ('SHA256_32z', 't_sha256_32', bytes(32)),
    ('SHA256_33z', 't_sha256_33', bytes(33)),
    ('SHA256_21z', 't_sha256_21', bytes(21)),
    ('SHA256_19z', 't_sha256_19', bytes(19)),
]:
    olen = 64 if '512' in name else 32
    got = run(K(func), data, olen).hex()
    exp = (hashlib.sha512 if '512' in name else hashlib.sha256)(data).hexdigest()
    status = 'PASS' if got == exp else 'FAIL'
    if status == 'FAIL': ok = False
    print(f"{name}: {status}")
    if status == 'FAIL':
        print(f"  got={got[:32]}...")
        print(f"  exp={exp[:32]}...")

if ok:
    # Save the new sha_kernels.cu
    with open('/home/hamsa/xrp_vanity_parts/sha_kernels.cu', 'w') as f:
        f.write(SHA_KERNELS)
    print("\nSaved updated sha_kernels.cu")
