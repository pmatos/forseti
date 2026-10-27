/* Hand-written bug-pair fixture (buggy twin of clamp.c): above the range it returns
 * `lo` instead of `hi`. Triggering input: x > hi, lo < hi (for example x=10, lo=0,
 * hi=5). S0 cannot see this; only a functional property can. */
int clamp(int x, int lo, int hi) {
    if (x < lo)
        return lo;
    if (x > hi)
        return lo;
    return x;
}
