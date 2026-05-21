"""Minimal: test the EXACT same algorithm in two forms - macros vs inline"""
import cupy as cp

PREAMBLE = "typedef unsigned int uint32_t;\n"

SRC = PREAMBLE + r"""
#define ROTR32(x,n) (((x)>>(n))|((x)<<(32-(n))))

// VERSION A: macros (like dbg_h_vals.py)
#define s0_A(x) (ROTR32(x,7)^ROTR32(x,18)^((x)>>3))
#define s1_A(x) (ROTR32(x,17)^ROTR32(x,19)^((x)>>10))
#define S0_A(x) (ROTR32(x,2)^ROTR32(x,13)^ROTR32(x,22))
#define S1_A(x) (ROTR32(x,6)^ROTR32(x,11)^ROTR32(x,25))
#define CH_A(x,y,z) (((x)&(y))^(~(x)&(z)))
#define MAJ_A(x,y,z) (((x)&(y))^((x)&(z))^((y)&(z)))

__device__ void sha256_macros(uint32_t *out) {
    uint32_t K[64] = {
        0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
        0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
        0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
        0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
        0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
        0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
        0x19a4c116u,0x1e376c08u,0x27487740u,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
        0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u
    };
    uint32_t W[64];
    for(int i=0;i<8;i++) W[i]=0u;
    W[8]=0x80000000u;
    for(int i=9;i<15;i++) W[i]=0u;
    W[15]=256u;
    for(int i=16;i<64;i++) W[i]=s1_A(W[i-2])+W[i-7]+s0_A(W[i-15])+W[i-16];

    uint32_t a=0x6a09e667u,b=0xbb67ae85u,c=0x3c6ef372u,d=0xa54ff53au;
    uint32_t e=0x510e527fu,f=0x9b05688cu,g=0x1f83d9abu,h=0x5be0cd19u;
    for(int i=0;i<64;i++){
        uint32_t t1=h+S1_A(e)+CH_A(e,f,g)+K[i]+W[i];
        uint32_t t2=S0_A(a)+MAJ_A(a,b,c);
        h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
    }
    out[0]=a; out[1]=b; out[2]=c; out[3]=d;
    out[4]=e; out[5]=f; out[6]=g; out[7]=h;
}

__device__ void sha256_inline(uint32_t *out) {
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

    uint32_t W[64];
    for(int i=0;i<8;i++) W[i]=0u;
    W[8]=0x80000000u;
    for(int i=9;i<15;i++) W[i]=0u;
    W[15]=256u;
    for(int i=16;i<64;i++) W[i]=(ROTR32(W[i-2],17)^ROTR32(W[i-2],19)^(W[i-2]>>10))+W[i-7]+(ROTR32(W[i-15],7)^ROTR32(W[i-15],18)^(W[i-15]>>3))+W[i-16];

    uint32_t a=0x6a09e667u,b=0xbb67ae85u,c=0x3c6ef372u,d=0xa54ff53au;
    uint32_t e=0x510e527fu,f=0x9b05688cu,g=0x1f83d9abu,h=0x5be0cd19u;

    for(uint32_t i=0;i<64u;i++){
        uint32_t t1=h+(ROTR32(e,6)^ROTR32(e,11)^ROTR32(e,25))+((e&f)^(~e&g))+K[i]+W[i];
        uint32_t t2=(ROTR32(a,2)^ROTR32(a,13)^ROTR32(a,22))+((a&b)^(a&c)^(b&c));
        h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
    }
    out[0]=a; out[1]=b; out[2]=c; out[3]=d;
    out[4]=e; out[5]=f; out[6]=g; out[7]=h;
}

extern "C" __global__ void test(uint32_t *out_macros, uint32_t *out_inline) {
    sha256_macros(out_macros);
    sha256_inline(out_inline);
}
"""

k = cp.RawKernel(SRC, 'test')
om = cp.zeros(8, dtype=cp.uint32)
oi = cp.zeros(8, dtype=cp.uint32)
k((1,),(1,),(om, oi))
m = [int(x) for x in om.get()]
i = [int(x) for x in oi.get()]

# Expected final state BEFORE adding h0..h7
# h_final = 0x66687aadf862bd776c8fc18b8e9f8e2008971485 6ee233b3 902a591d 0d5f2925
# Initial: h0=0x6a09e667 ...
H0 = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]
Hf = [0x66687aad,0xf862bd77,0x6c8fc18b,0x8e9f8e20,0x08971485,0x6ee233b3,0x902a591d,0x0d5f2925]
exp = [(hf-h0)&0xFFFFFFFF for h0,hf in zip(H0,Hf)]

names='abcdefgh'
print("MACROS:")
for n,v,e in zip(names,m,exp):
    print(f"  {n}: gpu={v:#010x} exp={e:#010x} {'OK' if v==e else 'FAIL'}")
print("INLINE:")
for n,v,e in zip(names,i,exp):
    print(f"  {n}: gpu={v:#010x} exp={e:#010x} {'OK' if v==e else 'FAIL'}")
