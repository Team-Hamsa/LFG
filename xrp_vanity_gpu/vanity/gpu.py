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


class VanityGpu:
    """Owns the compiled module and per-batch device/host buffers."""

    def __init__(self, batch_size: int):
        self.batch_size = batch_size
        self.module = compile_module()
        self._pipeline = self.module.get_function("pipeline")

        self._d_seeds = cp.zeros(batch_size * 16, dtype=cp.uint8)
        self._d_pubkeys = cp.zeros(batch_size * 33, dtype=cp.uint8)
        try:
            self._h_pubkeys = cp.cuda.alloc_pinned_memory(batch_size * 33)
            self._pinned = True
        except Exception:
            self._h_pubkeys = None
            self._pinned = False

        self._threads = 256
        self._blocks = (batch_size + self._threads - 1) // self._threads

    def run_batch(self, seeds: bytes) -> bytes:
        if len(seeds) != self.batch_size * 16:
            raise ValueError(
                f"seeds must be {self.batch_size * 16} bytes, got {len(seeds)}"
            )
        self._d_seeds.set(np.frombuffer(seeds, dtype=np.uint8))
        self._pipeline(
            (self._blocks,), (self._threads,),
            (self._d_seeds, self._d_pubkeys, np.uint32(self.batch_size)),
        )
        if self._pinned:
            cp.cuda.runtime.memcpy(
                int(self._h_pubkeys.ptr),
                int(self._d_pubkeys.data.ptr),
                self.batch_size * 33,
                cp.cuda.runtime.memcpyDeviceToHost,
            )
            return bytes(memoryview(self._h_pubkeys)[: self.batch_size * 33])
        return bytes(cp.asnumpy(self._d_pubkeys))
