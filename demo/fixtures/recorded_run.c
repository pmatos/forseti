#include <stdint.h>

int utf8_decode(const unsigned char *buf, unsigned len, uint32_t *codepoint)
{
    if (len < 1)
        return 0;

    unsigned char b0 = buf[0];

    if (b0 < 0x80) {
        *codepoint = b0;
        return 1;
    }

    if (b0 < 0xC2 || b0 > 0xF4)
        return 0;

    unsigned n;
    uint32_t cp;
    uint32_t min_cp;

    if (b0 < 0xE0) {
        n = 2;
        cp = b0 & 0x1F;
        min_cp = 0x80;
    } else if (b0 < 0xF0) {
        n = 3;
        cp = b0 & 0x0F;
        min_cp = 0x800;
    } else {
        n = 4;
        cp = b0 & 0x07;
        min_cp = 0x10000;
    }

    if (len < n)
        return 0;

    for (unsigned i = 1; i < n; i++) {
        unsigned char b = buf[i];
        if (b < 0x80 || b > 0xBF)
            return 0;
        cp = (cp << 6) | (b & 0x3F);
    }

    if (cp < min_cp || cp > 0x10FFFF)
        return 0;

    if (cp >= 0xD800 && cp <= 0xDFFF)
        return 0;

    *codepoint = cp;
    return (int)n;
}
