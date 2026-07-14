/* Demo unit for the Forseti verify-gate (a mechanism demo, like examples/abs.c).
 *
 * A bare function — no main, no harness. The gate verifies it function-level
 * with `esbmc --function my_abs --overflow-check`, which havocs `x` and finds
 * the INT64_MIN overflow: -INT64_MIN is not representable, so `-x` overflows.
 *
 * Edit `my_abs` to saturate INT64_MIN to INT64_MAX and the gate flips to
 * VERIFIED. Ask Claude to "make my_abs verify" with the plugin enabled to watch
 * the write -> verify -> counterexample -> fix loop close.
 */
#include <stdint.h>

int64_t my_abs(int64_t x) {
    return (x < 0) ? -x : x;
}
