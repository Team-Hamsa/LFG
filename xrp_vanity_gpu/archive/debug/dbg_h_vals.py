"""Output raw h values after SHA-256 compression to find bug"""
import hashlib, cupy as cp, struct

PREAMBLE = """
typedef unsigned char      uint8_t;
typedef unsigned int       uint32_t;
"""

SRC = PREAMBLE + r"""
#define ROTR32(x,n) (((x)>>(n))|((x)<<(32-(n))))
#define s0_256(x) (ROTR32(x,7)^ROTR32(x,18)^((x)>>3))
#define s1_256(x) (ROTR32(x,17)^ROTR32(x,19)^((x)>>10))
#define S0(x) (ROTR32(x,2)^ROTR32(x,13)^ROTR32(x,22))
#define S1(x) (ROTR32(x,6)^ROTR32(x,11)^ROTR32(x,25))
#define CH(x,y,z) (((x)&(y))^(~(x)&(z)))
#define MAJ(x,y,z) (((x)&(y))^((x)&(z))^((y)&(z)))

extern "C" __global__ void get_h_vals(uint32_t *hout) {
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
    for(int i=16;i<64;i++) W[i]=s1_256(W[i-2])+W[i-7]+s0_256(W[i-15])+W[i-16];

    uint32_t h0=0x6a09e667u,h1=0xbb67ae85u,h2=0x3c6ef372u,h3=0xa54ff53au;
    uint32_t h4=0x510e527fu,h5=0x9b05688cu,h6=0x1f83d9abu,h7=0x5be0cd19u;

    uint32_t a=h0,b=h1,c=h2,d=h3,e=h4,f=h5,g=h6,h=h7;
    for(int i=0;i<64;i++){
        uint32_t t1=h+S1(e)+CH(e,f,g)+K[i]+W[i];
        uint32_t t2=S0(a)+MAJ(a,b,c);
        h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
    }
    h0+=a; h1+=b; h2+=c; h3+=d; h4+=e; h5+=f; h6+=g; h7+=h;

    hout[0]=h0; hout[1]=h1; hout[2]=h2; hout[3]=h3;
    hout[4]=h4; hout[5]=h5; hout[6]=h6; hout[7]=h7;
}
"""

k = cp.RawKernel(SRC, 'get_h_vals')
hout = cp.zeros(8, dtype=cp.uint32)
k((1,),(1,),(hout,))
h_gpu = [int(x) for x in hout.get()]

# Expected h values for sha256(bytes(32))
# sha256(bytes(32)) = 66687aad f862bd77 6c8fc18b 8e9f8e20 08971485 6ee233b3 902a591d 0d5f2925
exp_hash = bytes.fromhex('66687aadf862bd776c8fc18b8e9f8e20089714856ee233b3902a591d0d5f2925')
h_ref = list(struct.unpack('>8I', exp_hash))

print("h values after compression:")
for i in range(8):
    match = 'OK' if h_gpu[i] == h_ref[i] else 'MISMATCH'
    print(f"  h{i}: gpu={h_gpu[i]:#010x}  ref={h_ref[i]:#010x}  {match}")

gpu_hash = ''.join(f'{x:08x}' for x in h_gpu)
exp_hash_hex = hashlib.sha256(bytes(32)).hexdigest()
print(f"\nGPU hash: {gpu_hash}")
print(f"Expected: {exp_hash_hex}")
