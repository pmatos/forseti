/* Staged-defect twin of examples/ring_buffer.c — a MECHANISM demo (it exercises
 * the VIOLATED -> counterexample plumbing, like examples/abs.c). The single
 * change: rb_push drops its `if (r->count == CAP) return 0;` full-check, so a
 * push into a full buffer still increments count. The capacity invariant
 * `count <= CAP` then breaks, and ESBMC returns the op sequence that overruns it.
 *
 * Verdict: VIOLATED at k=6. Counterexample: CAP+1 pushes with no intervening pop.
 *   forseti-esbmc examples/ring_buffer_bug.c -k 6   # exit 1
 *
 * NB this is a deliberately introduced bug for the harness demo. The corpus
 * TARGET is the clean examples/ring_buffer.c; per docs/design/0002 we never stage
 * defects in the clean kernels. */
#include <assert.h>

#define CAP 4
#define OPS 6

int nondet_int(void);

typedef struct {
    int buf[CAP];
    unsigned head, tail, count;
} ring_t;

void rb_init(ring_t *r) {
    r->head = r->tail = r->count = 0;
}

int rb_push(ring_t *r, int v) {
    /* BUG: missing `if (r->count == CAP) return 0;` — push never refuses. */
    r->buf[r->tail] = v;
    r->tail = (r->tail + 1) % CAP;
    r->count++;
    return 1;
}

int rb_pop(ring_t *r, int *out) {
    if (r->count == 0) return 0;
    *out = r->buf[r->head];
    r->head = (r->head + 1) % CAP;
    r->count--;
    return 1;
}

int main(void) {
    ring_t r;
    rb_init(&r);
    for (int i = 0; i < OPS; i++) {
        if (nondet_int()) {
            rb_push(&r, nondet_int());
        } else {
            int out;
            rb_pop(&r, &out);
        }
        assert(r.count <= CAP);
        assert(r.head < CAP && r.tail < CAP);
    }
    return 0;
}
