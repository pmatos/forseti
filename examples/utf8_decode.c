/* Decode one UTF-8 scalar from a byte buffer. The kernel (utf8_decode) is
 * portable C returning the number of bytes consumed (1..4) or 0 on any invalid/
 * truncated input; main is the ESBMC harness.
 *
 * The harness sizes the buffer to exactly `len` (a VLA), so a decoder that reads
 * a continuation byte past the provided length is a genuine out-of-bounds read
 * that ESBMC's default array-bounds checking catches — not merely a logical
 * over-read into slack. Properties on a successful decode: bytes consumed <= len,
 * the codepoint is <= U+10FFFF, and it is not a UTF-16 surrogate. The decoder
 * also rejects overlong encodings (codepoint below the minimum for its length).
 *
 * Verdict: VERIFIED up to k=4 (the continuation loop runs <= 3 times; k=4 keeps
 * the post-decode asserts reachable — confirmed non-vacuous with a temporary
 * assert(0), and the n>0 success branch is reachable).
 *   forseti-esbmc examples/utf8_decode.c -k 4
 *
 * Latent edge case: accepting overlong encodings (e.g. C0 80 for U+0000),
 * accepting surrogates U+D800..U+DFFF, or reading a continuation byte past the
 * end of a truncated multibyte sequence. The last is staged in
 * examples/utf8_decode_bug.c. */
#include <assert.h>
#include <stdint.h>

unsigned nondet_uint(void);
unsigned char nondet_uchar(void);

int utf8_decode(const unsigned char *b, unsigned len, uint32_t *cp) {
    if (len == 0) return 0;
    unsigned char lead = b[0];
    uint32_t c, min;
    int n;
    if (lead < 0x80) { *cp = lead; return 1; }          /* ASCII */
    else if ((lead & 0xE0) == 0xC0) { c = lead & 0x1F; n = 2; min = 0x80; }
    else if ((lead & 0xF0) == 0xE0) { c = lead & 0x0F; n = 3; min = 0x800; }
    else if ((lead & 0xF8) == 0xF0) { c = lead & 0x07; n = 4; min = 0x10000; }
    else return 0;                                      /* stray 0x80..0xBF or 0xF8..0xFF */

    for (int i = 1; i < n; i++) {
        if ((unsigned)i >= len) return 0;               /* truncated */
        unsigned char cb = b[i];
        if ((cb & 0xC0) != 0x80) return 0;              /* bad continuation byte */
        c = (c << 6) | (cb & 0x3F);
    }
    if (c < min) return 0;                              /* overlong */
    if (c >= 0xD800 && c <= 0xDFFF) return 0;           /* surrogate */
    if (c > 0x10FFFF) return 0;                         /* out of Unicode range */
    *cp = c;
    return n;
}

int main(void) {
    unsigned len = nondet_uint();
    __ESBMC_assume(len >= 1 && len <= 4);
    unsigned char b[len];
    for (unsigned i = 0; i < len; i++) b[i] = nondet_uchar();

    uint32_t cp = 0;
    int n = utf8_decode(b, len, &cp);
    if (n > 0) {
        assert((unsigned)n <= len);
        assert(cp <= 0x10FFFF);
        assert(cp < 0xD800 || cp > 0xDFFF);
    }
    return 0;
}
