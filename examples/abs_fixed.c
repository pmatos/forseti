/* The fix for examples/abs.c. -INT64_MIN is not representable, so we saturate
 * that one input to INT64_MAX instead of overflowing. my_abs is now >= 0 for
 * every int64_t, and ESBMC reports VERIFIED up to the bound. */
#include <stdint.h>
#include <assert.h>

int64_t nondet_int64(void);

int64_t my_abs(int64_t x) {
    if (x == INT64_MIN) return INT64_MAX;
    return (x < 0) ? -x : x;
}

int main(void) {
    int64_t x = nondet_int64();
    assert(my_abs(x) >= 0);
    return 0;
}
