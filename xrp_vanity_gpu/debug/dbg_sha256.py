import hashlib, cupy as cp

PREAMBLE = """
typedef unsigned char      uint8_t;
typedef unsigned int       uint32_t;
typedef unsigned long long uint64_t;
typedef signed int         int32_t;
"""

sha_code = open('/home/hamsa/xrp_vanity_parts/sha_kernels.cu').read()
src = PREAMBLE + sha_code + r"""
extern "C" __global__ void t_sha256_32(const uint8_t *i, uint8_t *o) { sha256_32(i, o); }
"""

k = cp.RawKernel(src, 't_sha256_32')
inp = cp.zeros(32, dtype=cp.uint8)
out = cp.zeros(32, dtype=cp.uint8)
k((1,),(1,),(inp, out))
got = out.get().tobytes().hex()
exp = hashlib.sha256(bytes(32)).hexdigest()
print("got:", got)
print("exp:", exp)
print("match:", got == exp)
