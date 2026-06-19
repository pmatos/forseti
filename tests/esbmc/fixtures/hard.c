/* Nonlinear over 64-bit values within bounds that fit (no wraparound), so the
 * bit-vector solver cannot decide it quickly -> reliably exercises a timeout. */
int main(void) {
  unsigned long a, b, c;
  __ESBMC_assume(a > 2 && b > 2 && c > 2 && a < 100000 && b < 100000 && c < 100000);
  if (a * a * a + b * b * b == c * c * c) {
    __ESBMC_assert(0, "no fermat-3 solution exists");
  }
  return 0;
}
