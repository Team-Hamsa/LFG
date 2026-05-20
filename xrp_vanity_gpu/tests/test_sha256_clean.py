"""Test a clean macro-free SHA-256 implementation"""
import hashlib, cupy as cp

PREAMBLE = """
typedef unsigned char      uint8_t;
typedef unsigned int       uint32_t;
typedef unsigned long long uint64_t;
typedef signed int         int32_t;
"""

# Clean single-function SHA-256, no macros, K inline as local array
SHA256_CLEAN = r"""
__device__ static void sha256_block(uint32_t *W, uint32_t *hh) {
    // W[0..15] must be pre-loaded; this expands schedule and compresses
    uint32_t K[64];
    K[0]=0x428a2f98u;K[1]=0x71374491u;K[2]=0xb5c0fbcfu;K[3]=0xe9b5dba5u;
    K[4]=0x3956c25bu;K[5]=0x59f111f1u;K[6]=0x923f82a4u;K[7]=0xab1c5ed5u;
    K[8]=0xd807aa98u;K[9]=0x12835b01u;K[10]=0x243185beu;K[11]=0x550c7dc3u;
    K[12]=0x72be5d74u;K[13]=0x80deb1feu;K[14]=0x9bdc06a7u;K[15]=0xc19bf174u;
    K[16]=0xe49b69c1u;K[17]=0xefbe4786u;K[18]=0x0fc19dc6u;K[19]=0x240ca1ccu;
    K[20]=0x2de92c6fu;K[21]=0x4a7484aau;K[22]=0x5cb0a9dcu;K[23]=0x76f988dau;
    K[24]=0x983e5152u;K[25]=0xa831c66du;K[26]=0xb00327c8u;K[27]=0xbf597fc7u;
    K[28]=0xc6e00bf3u;K[29]=0xd5a79147u;K[30]=0x06ca6351u;K[31]=0x14292967u;
    K[32]=0x27b70a85u;K[33]=0x2e1b2138u;K[34]=0x4d2c6dfcu;K[35]=0x53380d13u;
    K[36]=0x650a7354u;K[37]=0x766a0abbu;K[38]=0x81c2c92eu;K[39]=0x92722c85u;
    K[40]=0xa2bfe8a1u;K[41]=0xa81a664bu;K[42]=0xc24b8b70u;K[43]=0xc76c51a3u;
    K[44]=0xd192e819u;K[45]=0xd6990624u;K[46]=0xf40e3585u;K[47]=0x106aa070u;
    K[48]=0x19a4c116u;K[49]=0x1e376c08u;K[50]=0x27487740u;K[51]=0x34b0bcb5u;
    K[52]=0x391c0cb3u;K[53]=0x4ed8aa4au;K[54]=0x5b9cca4fu;K[55]=0x682e6ff3u;
    K[56]=0x748f82eeu;K[57]=0x78a5636fu;K[58]=0x84c87814u;K[59]=0x8cc70208u;
    K[60]=0x90befffau;K[61]=0xa4506cebu;K[62]=0xbef9a3f7u;K[63]=0xc67178f2u;

    #define ROTR32(x,n) (((x)>>(n))|((x)<<(32-(n))))
    #define S0(x) (ROTR32(x,2)^ROTR32(x,13)^ROTR32(x,22))
    #define S1(x) (ROTR32(x,6)^ROTR32(x,11)^ROTR32(x,25))
    #define s0(x) (ROTR32(x,7)^ROTR32(x,18)^((x)>>3))
    #define s1(x) (ROTR32(x,17)^ROTR32(x,19)^((x)>>10))
    #define CH(x,y,z) (((x)&(y))^(~(x)&(z)))
    #define MAJ(x,y,z) (((x)&(y))^((x)&(z))^((y)&(z)))

    for(int i=16;i<64;i++) W[i]=s1(W[i-2])+W[i-7]+s0(W[i-15])+W[i-16];

    uint32_t a=hh[0],b=hh[1],c=hh[2],d=hh[3];
    uint32_t e=hh[4],f=hh[5],g=hh[6],h=hh[7];
    for(int i=0;i<64;i++){
        uint32_t t1=h+S1(e)+CH(e,f,g)+K[i]+W[i];
        uint32_t t2=S0(a)+MAJ(a,b,c);
        h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
    }
    hh[0]+=a; hh[1]+=b; hh[2]+=c; hh[3]+=d;
    hh[4]+=e; hh[5]+=f; hh[6]+=g; hh[7]+=h;
    #undef ROTR32
    #undef S0
    #undef S1
    #undef s0
    #undef s1
    #undef CH
    #undef MAJ
}

__device__ static void sha256_init(uint32_t *hh) {
    hh[0]=0x6a09e667u; hh[1]=0xbb67ae85u; hh[2]=0x3c6ef372u; hh[3]=0xa54ff53au;
    hh[4]=0x510e527fu; hh[5]=0x9b05688cu; hh[6]=0x1f83d9abu; hh[7]=0x5be0cd19u;
}
__device__ static void sha256_out(const uint32_t *hh, uint8_t *out) {
    for(int i=0;i<8;i++){
        out[i*4+0]=(uint8_t)(hh[i]>>24); out[i*4+1]=(uint8_t)(hh[i]>>16);
        out[i*4+2]=(uint8_t)(hh[i]>>8);  out[i*4+3]=(uint8_t)(hh[i]);
    }
}
#define LOAD32BE(b,i) (((uint32_t)(b)[(i)]<<24)|((uint32_t)(b)[(i)+1]<<16)|((uint32_t)(b)[(i)+2]<<8)|(uint32_t)(b)[(i)+3])

__device__ void sha256_19(const uint8_t *in, uint8_t *out) {
    uint32_t W[64], hh[8]; sha256_init(hh);
    W[0]=LOAD32BE(in,0); W[1]=LOAD32BE(in,4); W[2]=LOAD32BE(in,8); W[3]=LOAD32BE(in,12);
    W[4]=((uint32_t)in[16]<<24)|((uint32_t)in[17]<<16)|((uint32_t)in[18]<<8)|0x80u;
    for(int i=5;i<15;i++) W[i]=0; W[15]=152u;
    sha256_block(W,hh); sha256_out(hh,out);
}
__device__ void sha256_21(const uint8_t *in, uint8_t *out) {
    uint32_t W[64], hh[8]; sha256_init(hh);
    W[0]=LOAD32BE(in,0); W[1]=LOAD32BE(in,4); W[2]=LOAD32BE(in,8); W[3]=LOAD32BE(in,12);
    W[4]=LOAD32BE(in,16); W[5]=((uint32_t)in[20]<<24)|0x00800000u;
    for(int i=6;i<15;i++) W[i]=0; W[15]=168u;
    sha256_block(W,hh); sha256_out(hh,out);
}
__device__ void sha256_32(const uint8_t *in, uint8_t *out) {
    uint32_t W[64], hh[8]; sha256_init(hh);
    for(int i=0;i<8;i++) W[i]=LOAD32BE(in,i*4);
    W[8]=0x80000000u; for(int i=9;i<15;i++) W[i]=0; W[15]=256u;
    sha256_block(W,hh); sha256_out(hh,out);
}
__device__ void sha256_33(const uint8_t *in, uint8_t *out) {
    uint32_t W[64], hh[8]; sha256_init(hh);
    for(int i=0;i<8;i++) W[i]=LOAD32BE(in,i*4);
    W[8]=((uint32_t)in[32]<<24)|0x00800000u;
    for(int i=9;i<15;i++) W[i]=0; W[15]=264u;
    sha256_block(W,hh); sha256_out(hh,out);
}
"""

src = PREAMBLE + SHA256_CLEAN + r"""
extern "C" __global__ void t_sha256_32(const uint8_t *i, uint8_t *o) { sha256_32(i, o); }
extern "C" __global__ void t_sha256_33(const uint8_t *i, uint8_t *o) { sha256_33(i, o); }
extern "C" __global__ void t_sha256_21(const uint8_t *i, uint8_t *o) { sha256_21(i, o); }
extern "C" __global__ void t_sha256_19(const uint8_t *i, uint8_t *o) { sha256_19(i, o); }
"""

def run(name, inp, olen=32):
    k = cp.RawKernel(src, name)
    i = cp.frombuffer(inp, dtype=cp.uint8)
    o = cp.zeros(olen, dtype=cp.uint8)
    k((1,),(1,),(i,o))
    return o.get().tobytes().hex()

ok = True
for label, name, data in [
    ('SHA256_32(zeros)', 't_sha256_32', bytes(32)),
    ('SHA256_33(zeros)', 't_sha256_33', bytes(33)),
    ('SHA256_21(zeros)', 't_sha256_21', bytes(21)),
    ('SHA256_19(zeros)', 't_sha256_19', bytes(19)),
    ('SHA256_32(abc+z)',  't_sha256_32', b'abcdefghijklmnopqrstuvwxyz012345'),
]:
    got = run(name, data)
    exp = hashlib.sha256(data).hexdigest()
    status = 'PASS' if got == exp else 'FAIL'
    if status == 'FAIL': ok = False
    print(f"{label}: {status}")
    if status == 'FAIL':
        print(f"  got={got}")
        print(f"  exp={exp}")

if ok:
    print("\nAll pass! Saving sha_kernels.cu...")
    sha512_code = open('/home/hamsa/xrp_vanity_parts/sha_kernels.cu').read()
    # Extract just the sha512 part (everything before the SHA-256 section)
    # Actually we'll write a fresh file with both sha512 and the new sha256
    # First grab the sha512_16 function from the original
    pass
