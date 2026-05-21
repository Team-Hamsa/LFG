import pytest


@pytest.mark.gpu
def test_module_compiles():
    """Concatenated kernel source must compile under NVRTC."""
    from vanity import gpu
    mod = gpu.compile_module()
    assert mod is not None


@pytest.mark.gpu
def test_sha512_16_matches_hashlib():
    """GPU sha512_16 must produce the same digest as hashlib for known inputs."""
    import hashlib
    import cupy as cp
    import numpy as np
    from vanity import gpu

    mod = gpu.compile_module()
    k = mod.get_function("sha512_16_test")

    seeds = [b"\x00" * 16, b"\xff" * 16, b"XRPL vanity seed"]
    for seed in seeds:
        d_in = cp.asarray(np.frombuffer(seed, dtype=np.uint8))
        d_out = cp.zeros(64, dtype=cp.uint8)
        k((1,), (1,), (d_in, d_out))
        got = bytes(cp.asnumpy(d_out))
        expected = hashlib.sha512(seed).digest()
        assert got == expected, f"seed={seed.hex()}: got {got.hex()}, expected {expected.hex()}"


@pytest.mark.gpu
def test_sha512_32_matches_hashlib():
    """GPU sha512_32 must produce the same digest as hashlib for known inputs."""
    import hashlib
    import cupy as cp
    import numpy as np
    from vanity import gpu

    mod = gpu.compile_module()
    k = mod.get_function("sha512_32_test")

    import secrets
    for _ in range(5):
        data = secrets.token_bytes(32)
        d_in = cp.asarray(np.frombuffer(data, dtype=np.uint8))
        d_out = cp.zeros(64, dtype=cp.uint8)
        k((1,), (1,), (d_in, d_out))
        got = bytes(cp.asnumpy(d_out))
        expected = hashlib.sha512(data).digest()
        assert got == expected, f"data={data.hex()}: got {got.hex()}, expected {expected.hex()}"
