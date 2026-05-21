DONNA_DIR := ../third_party/ed25519-donna
# ED25519_REFHASH: use donna's bundled SHA-512 (no libssl dependency)
# ED25519_CUSTOMRANDOM: suppress random-bytes dependency (we don't sign)
CFLAGS    := -O2 -DED25519_REFHASH -DED25519_CUSTOMRANDOM -I$(DONNA_DIR)

all: dump_ed25519_vectors

dump_ed25519_vectors: dump_ed25519_vectors.c
	$(CC) $(CFLAGS) $< -o $@

clean:
	rm -f dump_ed25519_vectors
