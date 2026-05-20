import hashlib, sys
import cupy as cp
import base58

PREAMBLE = """
typedef unsigned char      uint8_t;
typedef unsigned short     uint16_t;
typedef unsigned int       uint32_t;
typedef unsigned long long uint64_t;
typedef signed char        int8_t;
typedef signed short       int16_t;
typedef signed int         int32_t;
typedef signed long long   int64_t;
"""

sha_code = open('/home/hamsa/xrp_vanity_parts/sha_kernels.cu').read()
b58_code = open('/home/hamsa/xrp_vanity_parts/base58_kernel.cu').read()
src = PREAMBLE + sha_code + b58_code + r"""
extern "C" __global__ void t_sha512(const uint8_t *i, uint8_t *o) { sha512_16(i, o); }
extern "C" __global__ void t_sha256_32(const uint8_t *i, uint8_t *o) { sha256_32(i, o); }
extern "C" __global__ void t_sha256_33(const uint8_t *i, uint8_t *o) { sha256_33(i, o); }
extern "C" __global__ void t_sha256_21(const uint8_t *i, uint8_t *o) { sha256_21(i, o); }
extern "C" __global__ void t_b58(const uint8_t *i, char *o) { base58_encode_address(i, o); }
extern "C" __global__ void t_icase(const char *a, const char *p, int n, int *r) { r[0]=contains_icase(a,p,n); }
"""

print("Compiling...", flush=True)

def run(k, inp, olen):
    i = cp.frombuffer(inp, dtype=cp.uint8)
    o = cp.zeros(olen, dtype=cp.uint8)
    k((1,),(1,),(i, o))
    return o.get().tobytes()

def K(name): return cp.RawKernel(src, name)

print("SHA512:", "PASS" if run(K('t_sha512'), bytes(16), 64).hex() == hashlib.sha512(bytes(16)).hexdigest() else "FAIL")
print("SHA256_32:", "PASS" if run(K('t_sha256_32'), bytes(32), 32).hex() == hashlib.sha256(bytes(32)).hexdigest() else "FAIL")
print("SHA256_33:", "PASS" if run(K('t_sha256_33'), bytes(33), 32).hex() == hashlib.sha256(bytes(33)).hexdigest() else "FAIL")
print("SHA256_21:", "PASS" if run(K('t_sha256_21'), bytes(21), 32).hex() == hashlib.sha256(bytes(21)).hexdigest() else "FAIL")

XRP = b'rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz'
data21 = bytes(21)
chk = hashlib.sha256(hashlib.sha256(data21).digest()).digest()[:4]
payload = data21 + chk
exp = base58.b58encode(payload, alphabet=XRP).decode()
i = cp.frombuffer(payload, dtype=cp.uint8)
o = cp.zeros(40, dtype=cp.uint8)
K('t_b58')((1,),(1,),(i, o))
got = o.get().tobytes().split(b'\x00')[0].decode()
print(f"Base58: {'PASS' if got==exp else 'FAIL'} got={got} exp={exp}")

addr = cp.frombuffer(b'rDaimyo123456789012345678\x00' + bytes(10), dtype=cp.int8)
pat  = cp.frombuffer(b'Daimyo', dtype=cp.int8)
res  = cp.zeros(1, dtype=cp.int32)
K('t_icase')((1,),(1,),(addr, pat, 6, res))
print("icase hit:", "PASS" if int(res[0])==1 else "FAIL")
pat2 = cp.frombuffer(b'XXXXXX', dtype=cp.int8)
K('t_icase')((1,),(1,),(addr, pat2, 6, res))
print("icase miss:", "PASS" if int(res[0])==0 else "FAIL")
print("All done.")
