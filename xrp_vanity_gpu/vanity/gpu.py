"""CuPy kernel-loading and launch wrappers."""
from pathlib import Path

import cupy as cp
import numpy as np

KERNELS_DIR = Path(__file__).resolve().parents[1] / "kernels"

# typedef preamble: NVRTC has no stdint.h.
NVRTC_PREAMBLE = """
typedef unsigned char       uint8_t;
typedef unsigned short      uint16_t;
typedef unsigned int        uint32_t;
typedef unsigned long long  uint64_t;
typedef long long           int64_t;
typedef signed char         int8_t;
"""

# Order matters: sha_kernels first (defines sha512_16/sha512_32),
# then ed25519 (depends on typedefs from preamble),
# then pipeline (depends on both).
KERNEL_FILES = ["sha_kernels.cu", "ed25519_kernel.cu", "pipeline_kernel.cu"]


def _read_kernel_sources() -> str:
    chunks = [NVRTC_PREAMBLE]
    for name in KERNEL_FILES:
        chunks.append((KERNELS_DIR / name).read_text(encoding="ascii", errors="replace"))
    return "\n".join(chunks)


def compile_module() -> cp.RawModule:
    """Compile the concatenated kernel source under NVRTC."""
    source = _read_kernel_sources()
    try:
        return cp.RawModule(
            code=source,
            backend="nvrtc",
            options=("--std=c++14",),
        )
    except cp.cuda.compiler.CompileException as e:
        numbered = "\n".join(
            f"{i+1:4d}: {line}" for i, line in enumerate(source.splitlines())
        )
        raise RuntimeError(
            f"NVRTC compile failed:\n{e}\n\n--- FULL SOURCE ---\n{numbered}"
        ) from e
