"""CLI tests: pattern validation, deterministic-seed e2e, integration smoke."""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "xrp_vanity_gpu.py"


def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = [
        "bash", "-lc",
        "source ~/miniconda3/etc/profile.d/conda.sh && "
        "conda run -n rapids-23.12 python " + str(CLI) + " " +
        " ".join(args),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def test_cli_rejects_illegal_pattern():
    # '0' is not in the XRPL base58 alphabet.
    res = _run(["0Foo", "--batch-size", "16", "--max-matches", "0"])
    assert res.returncode != 0
    assert "illegal" in (res.stdout + res.stderr).lower() or \
           "alphabet" in (res.stdout + res.stderr).lower()


@pytest.mark.gpu
@pytest.mark.slow
def test_cli_finds_one_char_prefix_match():
    # A single-char prefix should match almost immediately.
    res = _run([
        "D",
        "--batch-size", "1024",
        "--max-matches", "1",
        "--seed-rng-seed", "12345",
        "--stats-interval", "1",
    ], timeout=60)
    assert res.returncode == 0, res.stderr
    assert "MATCH" in res.stdout
    assert "  rD" in res.stdout  # address line begins with rD


@pytest.mark.gpu
@pytest.mark.slow
def test_cli_finds_match_matches_xrplpy():
    # Same as above but parse the match line and verify against xrpl-py.
    res = _run([
        "D",
        "--batch-size", "1024",
        "--max-matches", "1",
        "--seed-rng-seed", "99",
    ], timeout=60)
    assert res.returncode == 0, res.stderr
    line = next(l for l in res.stdout.splitlines() if "MATCH" in l)
    # [2026-05-21T20:00:00] MATCH  rDxxx  seed=sEdYYY  (attempt N)
    parts = line.split()
    address = parts[2]
    seed_b58 = parts[3].split("=", 1)[1]
    from xrpl.core.keypairs import derive_keypair, derive_classic_address
    pub_hex, _ = derive_keypair(seed_b58)
    assert derive_classic_address(pub_hex) == address
