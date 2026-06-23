/* Staged-defect twin of examples/murmurhash.c — a MECHANISM demo. The single
 * change: the block count is computed as `len/4 + 1` instead of `len/4`, so the
 * block loop processes one 4-byte block too many and reads key bytes past
 * key[len-1]. Because the harness sizes the buffer to exactly `len`, that
 * over-read is a genuine out-of-bounds read at every length (not only when a
 * fixed-size buffer happens to be exceeded), and ESBMC's default array-bounds
 * checking reports it.
 *
 * Verdict: VIOLATED at k=8 ("dereference failure: array bounds violated").
 * Counterexample: any key whose extra block reads past the len-sized object.
 *   forseti-esbmc examples/murmurhash_bug.c -k 8   # exit 1
 *
 * Deliberately introduced bug for the harness demo; the corpus TARGET is the
 * clean examples/murmurhash.c (docs/design/0002). */
#include <stdint.h>
#include <stddef.h>

#define MAXLEN 8

unsigned nondet_uint(void);
unsigned char nondet_uchar(void);

static uint32_t rotl32(uint32_t x, int8_t r) {
    return (x << r) | (x >> (32 - r));
}

static uint32_t murmur3_32(const uint8_t *key, size_t len, uint32_t seed) {
    uint32_t h = seed;
    const uint32_t c1 = 0xcc9e2d51, c2 = 0x1b873593;
    size_t nblocks = len / 4 + 1;   /* BUG: should be len / 4 — one block too many. */

    for (size_t i = 0; i < nblocks; i++) {
        const uint8_t *p = key + i * 4;
        uint32_t k = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                     ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
        k *= c1; k = rotl32(k, 15); k *= c2;
        h ^= k; h = rotl32(h, 13); h = h * 5 + 0xe6546b64;
    }

    const uint8_t *tail = key + nblocks * 4;
    uint32_t k1 = 0;
    switch (len & 3) {
        case 3: k1 ^= (uint32_t)tail[2] << 16; /* fallthrough */
        case 2: k1 ^= (uint32_t)tail[1] << 8;  /* fallthrough */
        case 1: k1 ^= (uint32_t)tail[0];
                k1 *= c1; k1 = rotl32(k1, 15); k1 *= c2; h ^= k1;
    }

    h ^= (uint32_t)len;
    h ^= h >> 16; h *= 0x85ebca6b; h ^= h >> 13; h *= 0xc2b2ae35; h ^= h >> 16;
    return h;
}

int main(void) {
    unsigned len = nondet_uint();
    __ESBMC_assume(len >= 1 && len <= MAXLEN);
    uint8_t key[len];
    for (unsigned i = 0; i < len; i++) key[i] = nondet_uchar();

    uint32_t h = murmur3_32(key, len, 0);
    (void)h;
    return 0;
}
