/* Staged-bug twin of sha1.c (RFC-0003 S2). Identical except for a one-character
 * off-by-one in `sha1_update`: the loop guard is `i <= len`, so the last
 * iteration reads `data[len]` — one past the `malloc(len)` object the synthesised
 * precondition materialises. `forseti synth --function sha1_update` must report
 * VIOLATED with a real `array bounds violated` counterexample (non-vacuous),
 * exactly as `abs.c`/`*_bug.c` are mechanism demos. No `main`. */
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t state[5];
    uint64_t count;
    uint8_t buffer[64];
} sha1_ctx;

static uint32_t rotl32(uint32_t x, int c) {
    return (x << c) | (x >> (32 - c));
}

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

void sha1_init(sha1_ctx *ctx) {
    ctx->state[0] = 0x67452301;
    ctx->state[1] = 0xEFCDAB89;
    ctx->state[2] = 0x98BADCFE;
    ctx->state[3] = 0x10325476;
    ctx->state[4] = 0xC3D2E1F0;
    ctx->count = 0;
}

void sha1_update(sha1_ctx *ctx, const uint8_t *data, size_t len) {
    for (size_t i = 0; i <= len; i++) {  /* BUG: `<=` reads data[len] (off by one) */
        ctx->buffer[ctx->count % 64] = data[i];
        ctx->count++;
    }
}

void sha1_final(sha1_ctx *ctx, uint8_t digest[20]) {
    for (int i = 0; i < 20; i++) {
        digest[i] = (uint8_t)(ctx->state[i >> 2] >> ((3 - (i & 3)) * 8));
    }
}
