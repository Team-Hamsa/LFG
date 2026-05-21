"""Step-by-step SHA-256 debug to find where it goes wrong"""
import hashlib, cupy as cp

PREAMBLE = """
typedef unsigned char      uint8_t;
typedef unsigned int       uint32_t;
typedef unsigned long long uint64_t;
typedef signed int         int32_t;
"""

# Test 1: Can the GPU correctly compute ROTR and the first round of SHA256?
DEBUG_SRC = PREAMBLE + r"""
#define ROTR32(x,n) (((x)>>(n))|((x)<<(32-(n))))

extern "C" __global__ void test_rotr(uint32_t *out) {
    // ROTR(0x80000000, 6) = 0x02000000
    out[0] = ROTR32(0x80000000u, 6u);
    // ROTR(0x80000000, 11) = 0x00040000
    out[1] = ROTR32(0x80000000u, 11u);
    // ROTR(0x80000000, 25) = 0x00000040
    out[2] = ROTR32(0x80000000u, 25u);
    // Sigma1(0x510e527f) = ROTR(x,6) ^ ROTR(x,11) ^ ROTR(x,25)
    uint32_t e = 0x510e527fu;
    out[3] = ROTR32(e,6u) ^ ROTR32(e,11u) ^ ROTR32(e,25u);
    // K[0] directly
    out[4] = 0x428a2f98u;
    // Check: can we store and read back?
    uint32_t tmp = 0x12345678u;
    out[5] = tmp;
    // Simulate first round of SHA-256(bytes(32))
    // Initial state:
    uint32_t a=0x6a09e667u,b=0xbb67ae85u,c=0x3c6ef372u,d=0xa54ff53au;
    uint32_t e2=0x510e527fu,f=0x9b05688cu,g=0x1f83d9abu,h=0x5be0cd19u;
    // W[0] = 0 (first word of 32 zero bytes)
    uint32_t W0 = 0u;
    uint32_t K0 = 0x428a2f98u;
    uint32_t ch = (e2 & f) ^ (~e2 & g);
    uint32_t sig1 = ROTR32(e2,6u) ^ ROTR32(e2,11u) ^ ROTR32(e2,25u);
    uint32_t T1 = h + sig1 + ch + K0 + W0;
    uint32_t sig0 = ROTR32(a,2u) ^ ROTR32(a,13u) ^ ROTR32(a,22u);
    uint32_t maj = (a&b) ^ (a&c) ^ (b&c);
    uint32_t T2 = sig0 + maj;
    // After round 0: new_a = T1+T2, new_e = d+T1
    out[6] = T1 + T2;  // new a
    out[7] = d + T1;   // new e
}
"""

k = cp.RawKernel(DEBUG_SRC, 'test_rotr')
out = cp.zeros(8, dtype=cp.uint32)
k((1,),(1,),(out,))
r = out.get()

print(f"ROTR(0x80000000,6)  = {r[0]:#010x}  expect 0x02000000")
print(f"ROTR(0x80000000,11) = {r[1]:#010x}  expect 0x00040000")
print(f"ROTR(0x80000000,25) = {r[2]:#010x}  expect 0x00000040")

# Compute expected Sigma1(0x510e527f) in Python
def rotr32(x, n): return ((x >> n) | (x << (32-n))) & 0xFFFFFFFF
e_val = 0x510e527f
sig1_exp = rotr32(e_val,6) ^ rotr32(e_val,11) ^ rotr32(e_val,25)
print(f"Sigma1(0x510e527f)  = {r[3]:#010x}  expect {sig1_exp:#010x}")
print(f"K[0]                = {r[4]:#010x}  expect 0x428a2f98")
print(f"Store/read 0x12345678 = {r[5]:#010x}  expect 0x12345678")

# Compute expected first round
def ch(x,y,z): return (x&y) ^ (~x&z)
def maj(x,y,z): return (x&y) ^ (x&z) ^ (y&z)
def sig0(x): return rotr32(x,2) ^ rotr32(x,13) ^ rotr32(x,22)
def sig1(x): return rotr32(x,6) ^ rotr32(x,11) ^ rotr32(x,25)

a,b,c,d,e,f,g,h = 0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19
K0, W0 = 0x428a2f98, 0
T1 = (h + sig1(e) + ch(e,f,g) + K0 + W0) & 0xFFFFFFFF
T2 = (sig0(a) + maj(a,b,c)) & 0xFFFFFFFF
new_a = (T1 + T2) & 0xFFFFFFFF
new_e = (d + T1) & 0xFFFFFFFF
print(f"Round 0 new_a       = {r[6]:#010x}  expect {new_a:#010x}")
print(f"Round 0 new_e       = {r[7]:#010x}  expect {new_e:#010x}")
