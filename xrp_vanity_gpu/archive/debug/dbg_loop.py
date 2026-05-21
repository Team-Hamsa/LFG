"""Test if loop-indexed K/W gives different result than hardcoded round 0"""
import cupy as cp
import numpy as np

SRC = """
typedef unsigned int uint32_t;
#define ROTR32(x,n) (((x)>>(n))|((x)<<(32-(n))))
#define S1(x) (ROTR32(x,6)^ROTR32(x,11)^ROTR32(x,25))
#define CH(x,y,z) (((x)&(y))^(~(x)&(z)))
#define S0(x) (ROTR32(x,2)^ROTR32(x,13)^ROTR32(x,22))
#define MAJ(x,y,z) (((x)&(y))^((x)&(z))^((y)&(z)))

extern "C" __global__ void loop_vs_hardcode(uint32_t *out) {
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
    for(int i=16;i<64;i++) {
        uint32_t w15=W[i-15], w2=W[i-2];
        W[i]=(ROTR32(w2,17)^ROTR32(w2,19)^(w2>>10))+W[i-7]+
             (ROTR32(w15,7)^ROTR32(w15,18)^(w15>>3))+W[i-16];
    }

    // Method 1: hardcoded single round
    {
        uint32_t a=0x6a09e667u,b=0xbb67ae85u,c=0x3c6ef372u,d=0xa54ff53au;
        uint32_t e=0x510e527fu,f=0x9b05688cu,g=0x1f83d9abu,h=0x5be0cd19u;
        uint32_t t1=h+S1(e)+CH(e,f,g)+0x428a2f98u+W[0];
        uint32_t t2=S0(a)+MAJ(a,b,c);
        h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
        out[0]=a; out[1]=e;
    }

    // Method 2: for loop, 1 iteration
    {
        uint32_t a=0x6a09e667u,b=0xbb67ae85u,c=0x3c6ef372u,d=0xa54ff53au;
        uint32_t e=0x510e527fu,f=0x9b05688cu,g=0x1f83d9abu,h=0x5be0cd19u;
        for(int i=0;i<1;i++){
            uint32_t t1=h+S1(e)+CH(e,f,g)+K[i]+W[i];
            uint32_t t2=S0(a)+MAJ(a,b,c);
            h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
        }
        out[2]=a; out[3]=e;
    }

    // Method 3: for loop, 64 iterations - output after round 0 only
    {
        uint32_t a=0x6a09e667u,b=0xbb67ae85u,c=0x3c6ef372u,d=0xa54ff53au;
        uint32_t e=0x510e527fu,f=0x9b05688cu,g=0x1f83d9abu,h=0x5be0cd19u;
        uint32_t save_a=0, save_e=0;
        for(int i=0;i<64;i++){
            uint32_t t1=h+S1(e)+CH(e,f,g)+K[i]+W[i];
            uint32_t t2=S0(a)+MAJ(a,b,c);
            h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
            if(i==0){ save_a=a; save_e=e; }
        }
        out[4]=save_a; out[5]=save_e;
        out[6]=a; out[7]=e;  // final a,e after 64 rounds
    }
}
"""

k = cp.RawKernel(SRC, 'loop_vs_hardcode')
out = cp.zeros(8, dtype=cp.uint32)
k((1,),(1,),(out,))
r = [int(x) for x in out.get()]

# Expected round 0 result
def rotr32(x,n): return ((x>>n)|(x<<(32-n)))&0xFFFFFFFF
a,b,c,d,e,f,g,h = 0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19
t1=(h+(rotr32(e,6)^rotr32(e,11)^rotr32(e,25))+((e&f)^(~e&g)&0xFFFFFFFF)+0x428a2f98+0)&0xFFFFFFFF
t2=((rotr32(a,2)^rotr32(a,13)^rotr32(a,22))+((a&b)^(a&c)^(b&c)))&0xFFFFFFFF
exp_a=(t1+t2)&0xFFFFFFFF
exp_e=(d+t1)&0xFFFFFFFF

print(f"Expected after round 0: a={exp_a:#010x} e={exp_e:#010x}")
print(f"Method 1 (hardcoded):   a={r[0]:#010x} e={r[1]:#010x}  {'MATCH' if r[0]==exp_a and r[1]==exp_e else 'MISMATCH'}")
print(f"Method 2 (loop 1 iter): a={r[2]:#010x} e={r[3]:#010x}  {'MATCH' if r[2]==exp_a and r[3]==exp_e else 'MISMATCH'}")
print(f"Method 3 (loop 64, @0): a={r[4]:#010x} e={r[5]:#010x}  {'MATCH' if r[4]==exp_a and r[5]==exp_e else 'MISMATCH'}")
print(f"Method 3 final (after 64 rounds): a={r[6]:#010x} e={r[7]:#010x}")
