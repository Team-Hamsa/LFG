/*
 * Reads 16-byte seeds from stdin; writes 32-byte compressed Edwards points
 * to stdout.  One (seed, point) pair per iteration.
 *
 * Pipeline (matches XRPL / xrpl-py):
 *   seed (16B) -> SHA-512(seed) -> pre_key = [:32] -> ed25519_publickey(pre_key) -> point
 *
 * ed25519_publickey internally does:
 *   SHA-512(pre_key) -> extsk; clamp extsk[:32]; expand256_modm; scalarmult; pack.
 *
 * Build:  cd tools && make
 */

#include <stdio.h>
#include <string.h>

/* Donna's public API. */
#include "../third_party/ed25519-donna/ed25519.h"

/* Pull in the implementation so ed25519_publickey is defined.
   ED25519_REFHASH / ED25519_CUSTOMRANDOM are set via Makefile CFLAGS. */
#include "../third_party/ed25519-donna/ed25519.c"

/* Stub: satisfies ED25519_CUSTOMRANDOM contract; we never sign. */
void ed25519_randombytes_unsafe(void *p, size_t len) {
    (void)p; (void)len;
}

static void sha512_one_shot(const unsigned char *in, size_t len, unsigned char out[64]) {
    /* Use donna's bundled SHA-512 (available when ED25519_REFHASH). */
    ed25519_hash(out, in, len);
}

int main(void) {
    unsigned char seed[16];
    unsigned char pre_key[64];   /* SHA-512(seed), we use [:32] */
    unsigned char point[32];

    while (fread(seed, 1, 16, stdin) == 16) {
        sha512_one_shot(seed, 16, pre_key);
        /* pre_key[:32] is the Ed25519 "secret key" in XRPL's scheme. */
        ed25519_publickey(pre_key, point);
        if (fwrite(point, 1, 32, stdout) != 32) return 1;
        fflush(stdout);
    }
    return 0;
}
