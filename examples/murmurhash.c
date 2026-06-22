/* MurmurHash3 x86 32-bit. The kernel (murmur3_32) is portable C; main is the
 * ESBMC harness. All hash arithmetic is unsigned, so the wraparound is defined
 * (no signed-overflow UB).
 *
 * Property: MEMORY SAFETY over a nondet key of nondet length (<= MAXLEN). The
 * 4-byte block loop and the switch(len & 3) tail must never read past
 * key[len-1]; ESBMC's default array-bounds checking guards every key[] read, so
 * a memory-safe kernel verifies with no user assertion and a buggy tail/block
 * bound is reported. We deliberately do NOT assert determinism (f(x)==f(x)):
 * proving two full hash computations bit-identical forces the bit-vector solver
 * to blast the entire multiply/rotate chain twice and equate it, which exhausts
 * memory even for a 4-byte key. Memory safety is both tractable and the real bug
 * surface for a hash's tail handling.
 *
 * The key is filled with eight unrolled nondet bytes (no fill loop), so the only
 * loop is the block loop (len/4 <= 2 iterations); a small k covers it and the
 * post-hash point stays reachable (confirmed non-vacuous with a temporary
 * assert(0), and the bug below is caught — which itself proves the reads run).
 *
 * Verdict: VERIFIED up to k=4.
 *   forseti-esbmc examples/murmurhash.c -k 4
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
    uint8_t key[MAXLEN];
    unsigned len = nondet_uint();
    __ESBMC_assume(len <= MAXLEN);
    key[0] = nondet_uchar(); key[1] = nondet_uchar();
    key[2] = nondet_uchar(); key[3] = nondet_uchar();
    key[4] = nondet_uchar(); key[5] = nondet_uchar();
    key[6] = nondet_uchar(); key[7] = nondet_uchar();

    /* Property is the memory safety of these reads (default array-bounds checks);
     * (void)h keeps the call live so the reads are not sliced away. */
    uint32_t h = murmur3_32(key, len, 0);
    (void)h;
    return 0;
}
