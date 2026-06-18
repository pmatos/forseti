int my_abs(int x) { return x < 0 ? -x : x; }

int main(void) {
  int x;
  if (x > -100 && x < 100) {
    int r = my_abs(x);
    __ESBMC_assert(r >= 0, "abs is non-negative");
  }
  return 0;
}
