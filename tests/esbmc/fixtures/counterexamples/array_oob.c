int main(void) {
  int a[4];
  unsigned i;
  __ESBMC_assume(i < 10);
  a[i] = 7;
  return 0;
}
