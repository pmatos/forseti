/* MurmurHash3 x86 32-bit. The kernel (murmur3_32) is portable C; main is the
 * ESBMC harness. All hash arithmetic is unsigned, so the wraparound is defined
 * (no signed-overflow UB).
 *
 * Property: MEMORY SAFETY over a nondet key of nondet length. The 4-byte block
 * loop and the switch(len & 3) tail must never read past key[len-1]; ESBMC's
 * default array-bounds checking guards every key[] read. The harness sizes the
 * buffer to exactly `len` (a VLA), so a tail/block bug that over-reads the
 * logical input is a genuine out-of-bounds error even when it lands in what
 * would otherwise be slack bytes — the bound proven is key[0..len-1], not a
 * looser key[0..MAXLEN-1]. This mirrors examples/utf8_decode.c. We deliberately
 * do NOT assert determinism (f(x)==f(x)): proving two full hash computations
 * bit-identical forces the bit-vector solver to blast the entire multiply/rotate
 * chain twice and equate it, which exhausts memory. Memory safety is both
 * tractable and the real bug surface for a hash's tail handling.
 *
 * Verdict: VERIFIED up to k=8 (the fill loop runs len <= MAXLEN times, so k must
 * cover it; non-vacuity confirmed with a temporary assert(0) at the post-hash
 * point, and the bug below is caught — which itself proves the reads run).
 *   forseti-esbmc examples/murmurhash.c -k 8
 *
 * Latent edge case: the switch(len & 3) tail with its intentional fallthrough is
 * easy to get wrong, and a block loop using `<=` (or nblocks off by one) reads
 * one block too many — an out-of-bounds read. Staged in examples/murmurhash_bug.c. */
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
    size_t nblocks = len / 4;

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
    uint8_t key[len];                       /* sized to the logical length */
    for (unsigned i = 0; i < len; i++) key[i] = nondet_uchar();

    /* Property is the memory safety of murmur3_32's reads (default array-bounds
     * checks against the len-sized object); (void)h keeps the call live so the
     * reads are not sliced away. */
    uint32_t h = murmur3_32(key, len, 0);
    (void)h;
    return 0;
}
