"""End-to-end pipeline kernel test against xrpl-py.

For 1000 random 16-byte seeds, the pipeline kernel's 33-byte output
must equal the public-key bytes from xrpl-py's derive_keypair.
"""
import secrets

import cupy as cp
import numpy as np
import pytest

from vanity import encoding, gpu


def _xrplpy_pubkey(seed16: bytes) -> bytes:
    from xrpl.core.keypairs import derive_keypair
    s = encoding.family_seed_encode(seed16)
    pub_hex, _ = derive_keypair(s)
    return bytes.fromhex(pub_hex)


@pytest.mark.gpu
@pytest.mark.slow
def test_pipeline_kernel_1000_seeds():
    mod = gpu.compile_module()
    k = mod.get_function("pipeline")

    N = 1000
    seeds = b"".join(secrets.token_bytes(16) for _ in range(N))
    expected = [_xrplpy_pubkey(seeds[i * 16 : (i + 1) * 16]) for i in range(N)]

    d_seeds = cp.asarray(np.frombuffer(seeds, dtype=np.uint8))
    d_out = cp.zeros(N * 33, dtype=cp.uint8)

    threads = 128
    blocks = (N + threads - 1) // threads
    k((blocks,), (threads,), (d_seeds, d_out, np.uint32(N)))

    out = cp.asnumpy(d_out)
    mismatches = []
    for i in range(N):
        got = bytes(out[i * 33 : (i + 1) * 33])
        if got != expected[i]:
            mismatches.append((i, got.hex(), expected[i].hex()))
            if len(mismatches) > 5:
                break
    assert not mismatches, f"first mismatches: {mismatches}"
