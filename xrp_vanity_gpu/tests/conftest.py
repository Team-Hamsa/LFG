import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "gpu: requires a CUDA-capable GPU and CuPy at import time",
    )
    config.addinivalue_line(
        "markers",
        "slow: takes more than a few seconds (e.g. 1000-vector loops)",
    )
