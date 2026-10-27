/* Hand-written bug-pair fixture (clean). No memory access, loop or overflow, so the
 * safety checks (S0) pass and a property set can tell this from clamp_bug.c. */
int clamp(int x, int lo, int hi) {
    if (x < lo)
        return lo;
    if (x > hi)
        return hi;
    return x;
}
