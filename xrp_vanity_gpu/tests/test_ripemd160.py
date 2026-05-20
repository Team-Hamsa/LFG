"""Test RIPEMD-160 CUDA implementation against hashlib."""
import cupy as cp
import hashlib

# Read the kernel sources
with open('/home/hamsa/xrp_vanity_parts/sha_kernels.cu') as f:
    sha_src = f.read()
with open('/home/hamsa/xrp_vanity_parts/ripemd160_kernel.cu') as f:
    rmd_src = f.read()

kernel_code = sha_src + rmd_src + r"""
extern "C" __global__ void test_ripemd160(
    const uint8_t *inputs,   // n*32 bytes
    uint8_t *outputs,        // n*20 bytes
    int n
) {
    int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= n) return;
    ripemd160_32(inputs + tid*32, outputs + tid*20);
}
"""

mod = cp.RawModule(code=kernel_code, options=('--std=c++14',),
                   name_expressions=['test_ripemd160'])
fn = mod.get_function('test_ripemd160')

test_inputs = [
    bytes(32),
    b'hello world' + bytes(21),
    bytes(range(32)),
    hashlib.sha256(b'xrp').digest(),
]

n = len(test_inputs)
inp_flat = b''.join(test_inputs)
d_in  = cp.frombuffer(inp_flat, dtype=cp.uint8).copy()
d_out = cp.zeros(n * 20, dtype=cp.uint8)

fn((1,), (n,), (d_in, d_out, n))
cp.cuda.Stream.null.synchronize()

results = d_out.get()
passed = 0
failed = 0
for i, data in enumerate(test_inputs):
    expected = hashlib.new('ripemd160', data).digest()
    got = bytes(results[i*20:(i+1)*20])
    status = "PASS" if got == expected else "FAIL"
    if got == expected:
        passed += 1
    else:
        failed += 1
    print(f"[{status}] input={data[:8].hex()}... expected={expected.hex()} got={got.hex()}")

print(f"\n{passed}/{n} tests passed")
if failed:
    raise SystemExit(1)
