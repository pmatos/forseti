/* abs over int64_t. Looks correct, but isn't: -INT64_MIN is not representable,
 * so my_abs(INT64_MIN) returns INT64_MIN — still negative. ESBMC finds it and
 * reports VIOLATED with the counterexample x = INT64_MIN. */
#include <stdint.h>
#include <assert.h>

int64_t nondet_int64(void);

int64_t my_abs(int64_t x) {
    return (x < 0) ? -x : x;
}

int main(void) {
    int64_t x = nondet_int64();
    assert(my_abs(x) >= 0);
    return 0;
}
