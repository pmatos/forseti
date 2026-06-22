/* Staged-defect twin of examples/merge_sort.c — a MECHANISM demo. The single
 * change: merge drops its second drain loop `while (j < hi) tmp[k++] = a[j++];`.
 * When the left run is exhausted before the right, the leftover right-hand
 * elements are never copied into tmp, so stale tmp entries get written back and
 * the result is no longer sorted. ESBMC returns the input that triggers it.
 *
 * Verdict: VIOLATED at k=5. Counterexample: an input whose right half is the one
 * still holding elements when the merge's main loop ends.
 *   forseti-esbmc examples/merge_sort_bug.c -k 5   # exit 1
 *
 * Deliberately introduced bug for the harness demo; the corpus TARGET is the
 * clean examples/merge_sort.c (docs/design/0002). */
#include <assert.h>

#define N 4

int nondet_int(void);

static void merge(int *a, int lo, int mid, int hi, int *tmp) {
    int i = lo, j = mid, k = lo;
    while (i < mid && j < hi) tmp[k++] = (a[i] <= a[j]) ? a[i++] : a[j++];
    while (i < mid) tmp[k++] = a[i++];
    /* BUG: missing `while (j < hi) tmp[k++] = a[j++];` — right tail not drained. */
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
