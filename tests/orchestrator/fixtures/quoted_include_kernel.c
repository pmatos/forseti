/* Same abs/INT64_MIN bug as examples/abs.c, but non-self-contained: it pulls
 * in a sibling quoted header. Proves ProviderFixPort's candidate write still
 * resolves #include "quoted_include_helper.h" after a fix round (issue #39).
 */
#include <stdint.h>
#include <assert.h>
#include "quoted_include_helper.h"

int64_t nondet_int64(void);

int64_t my_abs(int64_t x) {
    return (x < 0) ? -x : x;
}

int main(void) {
    int64_t x = nondet_int64();
    assert(my_abs(x) >= 0);
    return 0;
}
