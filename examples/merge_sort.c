/* Recursive merge sort over a fixed-size int array. The kernel (msort/merge) is
 * portable C; main is the ESBMC harness. It sorts N nondet elements and asserts
 * the full correctness spec: the output is SORTED and is a PERMUTATION of the
 * input (multiset equality), so a degenerate "return a constant sorted array"
 * cannot pass.
 *
 * Two harness details are load-bearing for bounded model checking:
 *   - Element values are constrained to [0, 8) via __ESBMC_assume. The
 *     permutation check is O(N^2) over the values; with full-width nondet ints
 *     the bit-vector solver runs out of memory even at N=4. Bounding the value
 *     domain keeps it decidable.
 *   - k must be N+1, NOT N. With --no-unwinding-assertions, k=N silently cuts the
 *     loop-exit paths (the over-bound unwinding assertion becomes an assumption),
 *     leaving the post-loop asserts UNREACHABLE -> a spurious VERIFIED. At k=N+1
 *     the asserts are reachable (verified with a temporary assert(0)).
 *
 * Verdict: VERIFIED up to k=5 (N=4).
 *   forseti-esbmc examples/merge_sort.c -k 5
 *
 * Latent edge case: merge boundary handling — dropping the trailing tail copy,
 * or `<` vs `<=` in the comparator. Staged in examples/merge_sort_bug.c. */
#include <assert.h>

#define N 4

int nondet_int(void);

static void merge(int *a, int lo, int mid, int hi, int *tmp) {
    int i = lo, j = mid, k = lo;
    while (i < mid && j < hi) tmp[k++] = (a[i] <= a[j]) ? a[i++] : a[j++];
    while (i < mid) tmp[k++] = a[i++];
    while (j < hi) tmp[k++] = a[j++];
    for (k = lo; k < hi; k++) a[k] = tmp[k];
}

static void msort(int *a, int lo, int hi, int *tmp) {
    if (hi - lo < 2) return;
    int mid = lo + (hi - lo) / 2;
    msort(a, lo, mid, tmp);
    msort(a, mid, hi, tmp);
    merge(a, lo, mid, hi, tmp);
}

int main(void) {
    int a[N], orig[N], tmp[N];
    for (int i = 0; i < N; i++) {
        a[i] = nondet_int();
        __ESBMC_assume(a[i] >= 0 && a[i] < 8);
        orig[i] = a[i];
    }

    msort(a, 0, N, tmp);

    for (int i = 0; i + 1 < N; i++)
        assert(a[i] <= a[i + 1]);                       /* sorted */

    for (int i = 0; i < N; i++) {                        /* permutation */
        int ci = 0, co = 0;
        for (int j = 0; j < N; j++) {
            ci += (orig[j] == orig[i]);
            co += (a[j] == orig[i]);
        }
        assert(ci == co);
    }
    return 0;
}
