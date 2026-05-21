/* pipeline: seed (16B) -> sha512_16 -> sha512_32[:32] -> clamp -> ed25519 -> pubkey (33B)
   Populated in Task 9 once ed25519_kernel.cu is complete.

   Stub no-op so the concatenated source compiles during Tasks 6-8. */

extern "C" __global__
void pipeline_noop(unsigned int n) {
    (void)n;
}
