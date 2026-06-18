#include <limits.h>

int my_abs(int x) { return x < 0 ? -x : x; }

int main(void) {
  int x;
  if (x == INT_MIN) {
    int r = my_abs(x); /* -INT_MIN overflows: arithmetic overflow on neg */
    __ESBMC_assert(r >= 0, "abs is non-negative");
  }
  return 0;
}
