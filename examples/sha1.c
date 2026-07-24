/* SHA-1-shaped pointer signatures — the memory-precondition corpus target
 * (RFC-0003 S2).
 *
 * Four verification units keyed `sha1.c::sha1_{init,transform,update,final}`,
 * each taking pointer parameters and each individually **memory-safe**. They are
 * the targets for signature-driven memory-precondition synthesis: at the
 * function level ESBMC passes each pointer an invalid object and (soundly) fails
 * the first dereference, so `forseti synth` materialises a valid backing object
 * per pointer and reports VERIFIED *assuming valid caller pointers* — the L0
 * shapes exercised here:
 *
 *   - `sha1_ctx *ctx`               -> one fresh `sha1_ctx` (scalar pointer)
 *   - `const uint8_t *data, len`    -> `malloc(len)`, symbolic length (ptr,len)
 *   - `uint8_t digest[20]`          -> `malloc(20)` (fixed array extent)
 *
 * NOT a working SHA-1: `sha1_update` buffers input but never flushes a full
 * block, `sha1_final` serialises the state but never appends the padding/length,
 * and nothing calls `sha1_transform` — so `init`->`update`->`final` yields a
 * digest of the IV, independent of the input. That is deliberate: the file
 * exercises the memory-precondition mechanism on *realistic pointer signatures*,
 * and each unit is checked for **memory safety in isolation only**, never for
 * computing a correct hash. A real digest would need a block/padding driver that
 * is out of scope here (and, under a fresh nondet `ctx`, would make the units far
 * more expensive to verify). The off-by-one twin is `sha1_bug.c`. No `main`: the
 * synthesised sidecar `#include`s this file.
 */
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t state[5];
    uint64_t count;      /* bytes buffered so far */
    uint8_t buffer[64];  /* current partial block */
} sha1_ctx;

static uint32_t rotl32(uint32_t x, int c) {
    return (x << c) | (x >> (32 - c));
}

/* The block compression function: 80 rounds over the 64-byte `ctx->buffer`. */
void sha1_transform(sha1_ctx *ctx) {
    uint32_t w[80];
    for (int i = 0; i < 16; i++) {
        w[i] = ((uint32_t)ctx->buffer[i * 4] << 24)
             | ((uint32_t)ctx->buffer[i * 4 + 1] << 16)
             | ((uint32_t)ctx->buffer[i * 4 + 2] << 8)
             | (uint32_t)ctx->buffer[i * 4 + 3];
    }
    for (int i = 16; i < 80; i++) {
        w[i] = rotl32(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
    }
    uint32_t a = ctx->state[0], b = ctx->state[1], c = ctx->state[2];
    uint32_t d = ctx->state[3], e = ctx->state[4];
    for (int i = 0; i < 80; i++) {
        uint32_t f, k;
        if (i < 20) {
            f = (b & c) | ((~b) & d);
            k = 0x5A827999;
        } else if (i < 40) {
            f = b ^ c ^ d;
            k = 0x6ED9EBA1;
        } else if (i < 60) {
            f = (b & c) | (b & d) | (c & d);
            k = 0x8F1BBCDC;
        } else {
            f = b ^ c ^ d;
            k = 0xCA62C1D6;
        }
        uint32_t t = rotl32(a, 5) + f + e + k + w[i];
        e = d;
        d = c;
        c = rotl32(b, 30);
        b = a;
        a = t;
    }
    ctx->state[0] += a;
    ctx->state[1] += b;
    ctx->state[2] += c;
    ctx->state[3] += d;
    ctx->state[4] += e;
}

/* Initialise the context to the SHA-1 IV. */
void sha1_init(sha1_ctx *ctx) {
    ctx->state[0] = 0x67452301;
    ctx->state[1] = 0xEFCDAB89;
    ctx->state[2] = 0x98BADCFE;
    ctx->state[3] = 0x10325476;
    ctx->state[4] = 0xC3D2E1F0;
    ctx->count = 0;
}

/* Buffer `len` bytes of `data` into the 64-byte ring; `count % 64` keeps every
 * write in bounds for any `count`, so the unit is memory-safe under `malloc(len)`
 * with a symbolic `len`. */
void sha1_update(sha1_ctx *ctx, const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; i++) {
        ctx->buffer[ctx->count % 64] = data[i];
        ctx->count++;
    }
}

/* Serialise the 160-bit state big-endian into the caller's 20-byte `digest`. */
void sha1_final(sha1_ctx *ctx, uint8_t digest[20]) {
    for (int i = 0; i < 20; i++) {
        digest[i] = (uint8_t)(ctx->state[i >> 2] >> ((3 - (i & 3)) * 8));
    }
}
