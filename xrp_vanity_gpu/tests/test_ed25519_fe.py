"""Unit-test the CUDA fe_* (field arithmetic) port against pure-Python
mod-2^255-19 arithmetic. Covers fe_mul, fe_sq, fe_add, fe_sub, fe_invert.
"""
import numpy as np
import pytest

import cupy as cp

from vanity import gpu

P = (1 << 255) - 19
MASK51 = (1 << 51) - 1


def _packed_limbs_to_int(limbs5) -> int:
    """5 x 51-bit limbs (little-endian) -> big int mod p."""
    return sum((int(v) & MASK51) << (51 * i) for i, v in enumerate(limbs5)) % P


def _int_to_packed_limbs(n: int):
    n %= P
    return [(n >> (51 * i)) & MASK51 for i in range(5)]


def _packed_inputs(ints):
    return np.array(
        [_int_to_packed_limbs(x) for x in ints], dtype=np.uint64
    ).reshape(-1)


@pytest.fixture(scope="module")
def module():
    return gpu.compile_module()


def _run_binary(module, fn_name, a_ints, b_ints):
    fn = module.get_function(fn_name)
    n = len(a_ints)
    d_a = cp.asarray(_packed_inputs(a_ints))
    d_b = cp.asarray(_packed_inputs(b_ints))
    d_out = cp.zeros(5 * n, dtype=cp.uint64)
    fn((n,), (1,), (d_a, d_b, d_out, np.uint32(n)))
    return cp.asnumpy(d_out).reshape(n, 5).tolist()


def _run_unary(module, fn_name, a_ints):
    fn = module.get_function(fn_name)
    n = len(a_ints)
    d_a = cp.asarray(_packed_inputs(a_ints))
    d_out = cp.zeros(5 * n, dtype=cp.uint64)
    fn((n,), (1,), (d_a, d_out, np.uint32(n)))
    return cp.asnumpy(d_out).reshape(n, 5).tolist()


def _random_ints(n, seed):
    rng = np.random.default_rng(seed)
    return [int.from_bytes(rng.bytes(32), "little") % P for _ in range(n)]


@pytest.mark.gpu
def test_fe_mul_matches_python(module):
    a, b = _random_ints(64, 0xC0FFEE), _random_ints(64, 0xBEEF)
    out = _run_binary(module, "fe_mul_test", a, b)
    for i, (ai, bi) in enumerate(zip(a, b)):
        assert _packed_limbs_to_int(out[i]) == (ai * bi) % P, f"row {i}"


@pytest.mark.gpu
def test_fe_sq_matches_python(module):
    a = _random_ints(64, 0x5EED)
    out = _run_unary(module, "fe_sq_test", a)
    for i, ai in enumerate(a):
        assert _packed_limbs_to_int(out[i]) == (ai * ai) % P, f"row {i}"


@pytest.mark.gpu
def test_fe_add_matches_python(module):
    a, b = _random_ints(64, 0xADD), _random_ints(64, 0xADD2)
    out = _run_binary(module, "fe_add_test", a, b)
    for i, (ai, bi) in enumerate(zip(a, b)):
        assert _packed_limbs_to_int(out[i]) == (ai + bi) % P, f"row {i}"


@pytest.mark.gpu
def test_fe_sub_matches_python(module):
    a, b = _random_ints(64, 0x50B), _random_ints(64, 0x50B2)
    out = _run_binary(module, "fe_sub_test", a, b)
    for i, (ai, bi) in enumerate(zip(a, b)):
        assert _packed_limbs_to_int(out[i]) == (ai - bi) % P, f"row {i}"


@pytest.mark.gpu
def test_fe_invert_matches_python(module):
    a = [x or 1 for x in _random_ints(64, 0x1217)]  # avoid 0
    out = _run_unary(module, "fe_invert_test", a)
    for i, ai in enumerate(a):
        assert _packed_limbs_to_int(out[i]) == pow(ai, P - 2, P), f"row {i}"
