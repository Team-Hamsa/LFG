"""Check W schedule and full state after compression"""
import hashlib, cupy as cp

PREAMBLE = """
typedef unsigned char      uint8_t;
typedef unsigned int       uint32_t;
typedef unsigned long long uint64_t;
"""

SRC = PREAMBLE + r"""
#define ROTR32(x,n) (((x)>>(n))|((x)<<(32-(n))))
#define s0(x) (ROTR32(x,7)^ROTR32(x,18)^((x)>>3))
#define s1(x) (ROTR32(x,17)^ROTR32(x,19)^((x)>>10))

extern "C" __global__ void get_w_schedule(uint32_t *W_out) {
    uint32_t W[64];
    // SHA-256 of 32 zero bytes: message block
    for(int i=0;i<8;i++) W[i]=0u;
    W[8]=0x80000000u;
    for(int i=9;i<15;i++) W[i]=0u;
    W[15]=256u;
    // Expand schedule
    for(int i=16;i<64;i++) W[i]=s1(W[i-2])+W[i-7]+s0(W[i-15])+W[i-16];
    for(int i=0;i<64;i++) W_out[i]=W[i];
}
"""

k = cp.RawKernel(SRC, 'get_w_schedule')
W_gpu = cp.zeros(64, dtype=cp.uint32)
k((1,),(1,),(W_gpu,))
W_g = [int(x) for x in W_gpu.get()]

# Python reference W schedule for sha256(bytes(32))
def rotr32(x,n): return ((x>>n)|(x<<(32-n)))&0xFFFFFFFF
def s0(x): return rotr32(x,7)^rotr32(x,18)^(x>>3)
def s1(x): return rotr32(x,17)^rotr32(x,19)^(x>>10)

W_ref = [0]*64
for i in range(8): W_ref[i]=0
W_ref[8]=0x80000000
for i in range(9,15): W_ref[i]=0
W_ref[15]=256
for i in range(16,64): W_ref[i]=(s1(W_ref[i-2])+W_ref[i-7]+s0(W_ref[i-15])+W_ref[i-16])&0xFFFFFFFF

mismatches = 0
for i in range(64):
    if W_g[i] != W_ref[i]:
        print(f"W[{i:2d}] MISMATCH: gpu={W_g[i]:#010x} ref={W_ref[i]:#010x}")
        mismatches += 1
        if mismatches > 5: print("..."); break

if mismatches == 0:
    print("W schedule: PASS - all 64 values match")
    print(f"W[16]={W_g[16]:#010x} W[17]={W_g[17]:#010x} W[18]={W_g[18]:#010x}")
else:
    print(f"W schedule: {mismatches} mismatches")
