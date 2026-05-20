"""Check GPU K values and run first few rounds manually"""
import cupy as cp

PREAMBLE = "typedef unsigned int uint32_t;\n"

SRC = PREAMBLE + r"""
extern "C" __global__ void get_k(uint32_t *out, int n) {
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
    for(int i=0;i<n;i++) out[i]=K[i];
}

extern "C" __global__ void run_n_rounds(uint32_t n_rounds, uint32_t *hout) {
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

    // sha256(bytes(32)) message block
    uint32_t W[64];
    for(int i=0;i<8;i++) W[i]=0u;
    W[8]=0x80000000u;
    for(int i=9;i<15;i++) W[i]=0u;
    W[15]=256u;

    #define ROTR32(x,n) (((x)>>(n))|((x)<<(32-(n))))
    for(int i=16;i<64;i++) W[i]=(ROTR32(W[i-2],17)^ROTR32(W[i-2],19)^(W[i-2]>>10))+W[i-7]+(ROTR32(W[i-15],7)^ROTR32(W[i-15],18)^(W[i-15]>>3))+W[i-16];

    uint32_t a=0x6a09e667u,b=0xbb67ae85u,c=0x3c6ef372u,d=0xa54ff53au;
    uint32_t e=0x510e527fu,f=0x9b05688cu,g=0x1f83d9abu,h=0x5be0cd19u;

    for(uint32_t i=0;i<n_rounds;i++){
        uint32_t t1=h+(ROTR32(e,6)^ROTR32(e,11)^ROTR32(e,25))+((e&f)^(~e&g))+K[i]+W[i];
        uint32_t t2=(ROTR32(a,2)^ROTR32(a,13)^ROTR32(a,22))+((a&b)^(a&c)^(b&c));
        h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
    }
    hout[0]=a; hout[1]=b; hout[2]=c; hout[3]=d;
    hout[4]=e; hout[5]=f; hout[6]=g; hout[7]=h;
}
"""

# Get K values from GPU
k_kernel = cp.RawKernel(SRC, 'get_k')
k_out = cp.zeros(16, dtype=cp.uint32)
import numpy as np
k_kernel((1,),(1,),(k_out, np.int32(16)))
k_gpu = [int(x) for x in k_out.get()]
k_expected = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
              0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174]
print("K values from GPU (first 16):")
for i in range(16):
    ok = 'OK' if k_gpu[i] == k_expected[i] else f'MISMATCH exp={k_expected[i]:#010x}'
    print(f"  K[{i:2d}] = {k_gpu[i]:#010x}  {ok}")

# Check state after 1, 2, 4, 8, 16 rounds
def rotr32(x,n): return ((x>>n)|(x<<(32-n)))&0xFFFFFFFF
def s0r(x): return rotr32(x,7)^rotr32(x,18)^(x>>3)
def s1r(x): return rotr32(x,17)^rotr32(x,19)^(x>>10)
K_ref = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
         0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
         0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
         0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
         0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
         0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
         0x19a4c116,0x1e376c08,0x27487740,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
         0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]

W_ref = [0]*64
for i in range(8): W_ref[i]=0
W_ref[8]=0x80000000
for i in range(9,15): W_ref[i]=0
W_ref[15]=256
for i in range(16,64): W_ref[i]=(s1r(W_ref[i-2])+W_ref[i-7]+s0r(W_ref[i-15])+W_ref[i-16])&0xFFFFFFFF

rn_kernel = cp.RawKernel(SRC, 'run_n_rounds')
print("\nState after N rounds (GPU vs Python):")
a,b,c,d,e,f,g,h = 0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19

for nrounds in [8, 16, 24, 32, 40, 48, 56, 60, 62, 63, 64]:
    # GPU
    hout = cp.zeros(8, dtype=cp.uint32)
    rn_kernel((1,),(1,),(np.uint32(nrounds), hout))
    gpu = [int(x) for x in hout.get()]

    # Python
    pa,pb,pc,pd,pe,pf,pg,ph = a,b,c,d,e,f,g,h
    for i in range(nrounds):
        t1=(ph+(rotr32(pe,6)^rotr32(pe,11)^rotr32(pe,25))+((pe&pf)^(~pe&pg))+K_ref[i]+W_ref[i])&0xFFFFFFFF
        t2=((rotr32(pa,2)^rotr32(pa,13)^rotr32(pa,22))+((pa&pb)^(pa&pc)^(pb&pc)))&0xFFFFFFFF
        ph,pg,pf,pe,pd,pc,pb,pa = pg,pf,pe,(pd+t1)&0xFFFFFFFF,pc,pb,pa,(t1+t2)&0xFFFFFFFF

    match = 'MATCH' if gpu[:8]==[pa,pb,pc,pd,pe,pf,pg,ph] else 'MISMATCH'
    print(f"  After {nrounds:2d} rounds: {match}")
    if match == 'MISMATCH':
        for j,(gv,rv) in enumerate(zip(gpu[:8],[pa,pb,pc,pd,pe,pf,pg,ph])):
            if gv != rv:
                names = 'abcdefgh'
                print(f"    {names[j]}: gpu={gv:#010x} py={rv:#010x}")
