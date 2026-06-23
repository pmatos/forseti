/* Staged-defect twin of examples/utf8_decode.c — a MECHANISM demo. The single
 * change: the continuation-byte loop drops its `if ((unsigned)i >= len) return 0;`
 * truncation guard. A multibyte lead byte (e.g. 0xF0, announcing a 4-byte
 * sequence) with fewer than n bytes actually present then reads b[i] past the
 * end of the len-sized buffer — an out-of-bounds read that ESBMC's default
 * array-bounds checking reports.
 *
 * Verdict: VIOLATED at k=4 ("dereference failure: array bounds violated").
 * Counterexample: a truncated multibyte sequence (lead byte present, a promised
 * continuation byte missing).
 *   forseti-esbmc examples/utf8_decode_bug.c -k 4   # exit 1
 *
 * Deliberately introduced bug for the harness demo; the corpus TARGET is the
 * clean examples/utf8_decode.c (docs/design/0002). */
#include <assert.h>
#include <stdint.h>

unsigned nondet_uint(void);
unsigned char nondet_uchar(void);

int utf8_decode(const unsigned char *b, unsigned len, uint32_t *cp) {
    if (len == 0) return 0;
    unsigned char lead = b[0];
    uint32_t c, min;
    int n;
    if (lead < 0x80) { *cp = lead; return 1; }
    else if ((lead & 0xE0) == 0xC0) { c = lead & 0x1F; n = 2; min = 0x80; }
    else if ((lead & 0xF0) == 0xE0) { c = lead & 0x0F; n = 3; min = 0x800; }
    else if ((lead & 0xF8) == 0xF0) { c = lead & 0x07; n = 4; min = 0x10000; }
    else return 0;

    for (int i = 1; i < n; i++) {
        /* BUG: missing `if ((unsigned)i >= len) return 0;` — reads past len. */
        unsigned char cb = b[i];
        if ((cb & 0xC0) != 0x80) return 0;
        c = (c << 6) | (cb & 0x3F);
    }
    if (c < min) return 0;
    if (c >= 0xD800 && c <= 0xDFFF) return 0;
    if (c > 0x10FFFF) return 0;
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
