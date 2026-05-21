"""End-to-end Ed25519 scalarmult_base kernel test against donna vectors.

The vectors store (seed_hex, point_hex). The kernel's scalarmult_base_test
runs expand256_modm + ge25519_scalarmult_base_niels + ge25519_pack on a
32-byte scalar. donna derives that scalar from the 16-byte seed exactly as
ed25519_publickey does:

    pre_key = SHA-512(seed)[:32]
    extsk   = SHA-512(pre_key)            # 64 bytes
    clamp   extsk[0] &= 248
            extsk[31] = (extsk[31] & 127) | 64
    scalar  = extsk[:32]

So we recompute the clamped scalar here and feed it to the kernel.
"""
import hashlib
import json
from pathlib import Path

import cupy as cp
import numpy as np
import pytest

from vanity import gpu

VECTORS = json.loads(
    (Path(__file__).parent / "data" / "ed25519_vectors.json").read_text()
)


def _scalar_from_seed(seed16: bytes) -> bytes:
    pre_key = hashlib.sha512(seed16).digest()[:32]
    extsk = bytearray(hashlib.sha512(pre_key).digest()[:32])
    extsk[0] &= 248
    extsk[31] = (extsk[31] & 127) | 64
    return bytes(extsk)


@pytest.mark.gpu
def test_scalarmult_zero_is_identity():
    """[0]B packs to the identity point 0x01,0x00,...,0x00."""
    mod = gpu.compile_module()
    k = mod.get_function("scalarmult_zero_test")
    d_out = cp.zeros(32, dtype=cp.uint8)
    k((1,), (1,), (d_out,))
    out = bytes(cp.asnumpy(d_out))
    expected = bytes([1] + [0] * 31)
    assert out == expected, out.hex()


@pytest.mark.gpu
@pytest.mark.slow
def test_scalarmult_base_all_1000_vectors():
    mod = gpu.compile_module()
    k = mod.get_function("scalarmult_base_test")

    N = len(VECTORS)
    scalars = np.frombuffer(
        b"".join(_scalar_from_seed(bytes.fromhex(v["seed_hex"])) for v in VECTORS),
        dtype=np.uint8,
    ).reshape(N, 32)
    expected = [bytes.fromhex(v["point_hex"]) for v in VECTORS]

    d_scalars = cp.asarray(scalars).reshape(-1)
    d_out = cp.zeros(N * 32, dtype=cp.uint8)

    threads = 128
    blocks = (N + threads - 1) // threads
    k((blocks,), (threads,), (d_scalars, d_out, np.uint32(N)))

    out = cp.asnumpy(d_out).reshape(N, 32)
    mismatches = []
    for i in range(N):
        if bytes(out[i]) != expected[i]:
            mismatches.append((i, bytes(out[i]).hex(), expected[i].hex()))
            if len(mismatches) > 5:
                break
    assert not mismatches, f"first mismatches: {mismatches}"
