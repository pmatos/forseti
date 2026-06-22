/* Bounded circular FIFO of fixed capacity CAP. The kernel (rb_init/rb_push/
 * rb_pop) is portable C; main is the ESBMC harness. It drives OPS nondet ops
 * (each a push or a pop) and, after every op, asserts the structural
 * invariants: the element count never exceeds CAP and head/tail stay in range.
 * ESBMC's default array-bounds checking additionally guards every buf[] access.
 *
 * Verdict: VERIFIED up to k=OPS (the op loop runs OPS times; k must cover it).
 *   forseti-esbmc examples/ring_buffer.c -k 6
 *
 * Latent edge case (the bug a writer slips into without being told to): the
 * full/empty boundary. Using head==tail to mean "empty" is ambiguous with
 * "full", and dropping the count==CAP guard lets a push overrun buf[]. The
 * staged version of exactly that lives in examples/ring_buffer_bug.c. */
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
    if (r->count == CAP) return 0;
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
