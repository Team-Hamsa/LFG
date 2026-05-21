import random
import secrets

import pytest

from vanity import encoding, gpu


def _xrplpy_pubkey(seed16: bytes) -> bytes:
    from xrpl.core.keypairs import derive_keypair
    s = encoding.family_seed_encode(seed16)
    pub_hex, _ = derive_keypair(s)
    return bytes.fromhex(pub_hex)


@pytest.mark.gpu
def test_vanity_gpu_run_batch():
    B = 256
    g = gpu.VanityGpu(batch_size=B)
    seeds = b"".join(secrets.token_bytes(16) for _ in range(B))
    pubkeys = g.run_batch(seeds)
    assert len(pubkeys) == B * 33
    for idx in random.sample(range(B), 5):
        expected = _xrplpy_pubkey(seeds[idx * 16 : (idx + 1) * 16])
        got = pubkeys[idx * 33 : (idx + 1) * 33]
        assert got == expected, f"row {idx} mismatch"
