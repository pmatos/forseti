/* The fix for quoted_include_kernel.c: route through the sibling header's
 * saturating helper instead of negating INT64_MIN directly. */
#include <stdint.h>
#include <assert.h>
#include "quoted_include_helper.h"

int64_t nondet_int64(void);

int64_t my_abs(int64_t x) {
    return saturate_abs(x);
}

int main(void) {
    int64_t x = nondet_int64();
    assert(my_abs(x) >= 0);
    return 0;
}
