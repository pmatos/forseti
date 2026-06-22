# Example corpus

The C kernels the loop (`write → verify → counterexample → fix`) targets. Each
file is self-contained: a **portable kernel function** plus a `main` **ESBMC
harness** (nondet inputs, `__ESBMC_assume` bounds, `assert` properties), in the
style of `abs.c`. Verification units are keyed `path::symbol`.

## Two kinds of file (per [RFC-0002](../docs/design/0002-eliciting-natural-bugs.md))

- **Clean kernels** (`ring_buffer.c`, `merge_sort.c`, `utf8_decode.c`,
  `murmurhash.c`) are correct reference implementations that come back
  **VERIFIED**. They are the *natural-bug* targets: tasks at the frontier of a
  writer's competence (ring wraparound, merge boundaries, UTF-8 truncation,
  hash tail handling) where a model errs **without being told to**. We never
  stage a defect in a clean file — the P1 loop and the P2 mutation harness
  surface bugs from these honestly.
- **Staged-bug twins** (`*_bug.c`) carry one deliberately introduced defect and
  come back **VIOLATED** with a counterexample. They are *mechanism demos* — unit
  tests of the harness plumbing (VIOLATED → read cex → fix → VERIFIED), exactly
  like `abs.c`. Staging here is legitimate because it makes no claim about what a
  model would write.

**Naming.** For the corpus the bare name is the **clean** kernel and `_bug` marks
the staged defect. This is the *opposite* of the older `abs.c` (buggy) /
`abs_fixed.c` (fixed) pair, which predates the corpus as a standalone demo.

## Catalog

| File | Unit (`path::symbol`) | Property | Verdict | Run |
|------|----------------------|----------|---------|-----|
| `ring_buffer.c` | `ring_buffer.c::rb_push` | count/index invariants hold across a nondet op sequence | VERIFIED (k=6) | `forseti-esbmc examples/ring_buffer.c -k 6` |
| `ring_buffer_bug.c` | `ring_buffer.c::rb_push` | — (full-check dropped) | VIOLATED (k=6) | `forseti-esbmc examples/ring_buffer_bug.c -k 6` |
| `merge_sort.c` | `merge_sort.c::msort` | output is sorted **and** a permutation of the input | VERIFIED (k=5) | `forseti-esbmc examples/merge_sort.c -k 5` |
| `merge_sort_bug.c` | `merge_sort.c::msort` | — (merge tail drain dropped) | VIOLATED (k=5) | `forseti-esbmc examples/merge_sort_bug.c -k 5` |
| `utf8_decode.c` | `utf8_decode.c::utf8_decode` | memory-safe; valid scalar, no surrogate, consumed ≤ len | VERIFIED (k=4) | `forseti-esbmc examples/utf8_decode.c -k 4` |
| `utf8_decode_bug.c` | `utf8_decode.c::utf8_decode` | — (`i<len` guard dropped) | VIOLATED (k=4) | `forseti-esbmc examples/utf8_decode_bug.c -k 4` |
| `murmurhash.c` | `murmurhash.c::murmur3_32` | memory safety over a nondet key/len (block + tail reads in bounds) | VERIFIED (k=4) | `forseti-esbmc examples/murmurhash.c -k 4` |
| `murmurhash_bug.c` | `murmurhash.c::murmur3_32` | — (`nblocks` off by one) | VIOLATED (k=4) | `forseti-esbmc examples/murmurhash_bug.c -k 4` |
| `abs.c` | `abs.c::my_abs` | `my_abs(x) >= 0` for every `int64_t` | VIOLATED (k=1) | `forseti-esbmc examples/abs.c` |
| `abs_fixed.c` | `abs_fixed.c::my_abs` | `my_abs(x) >= 0` for every `int64_t` | VERIFIED (k=1) | `forseti-esbmc examples/abs_fixed.c` |

### Latent edge case per kernel (the failure a writer slips into)

- **ring buffer** — the full/empty boundary: `head==tail` is ambiguous between
  empty and full, and a missing `count==CAP` guard overruns the capacity.
- **merge sort** — merge boundary handling: dropping the trailing tail drain, or
  `<` vs `<=` in the comparator. Sortedness alone misses a dropped element; the
  permutation check catches it.
- **utf8 decode** — accepting overlong encodings (`C0 80` for U+0000) or
  surrogates, or reading a continuation byte past a truncated sequence (OOB).
- **murmurhash** — the `switch(len & 3)` tail fallthrough, or a block count off by
  one reading a block past the key (OOB).

## Verification discipline — read before trusting a VERIFIED

`verify()` always runs ESBMC with `--unwind k --no-unwinding-assertions`. With
that flag a `k` **less than or equal to** a loop's trip count silently assumes the
loop exited, cutting the loop-exit path — so any property asserted *after* the
loop becomes unreachable and ESBMC reports a **spurious VERIFIED** (roadmap
Risk 1). Therefore, for every kernel whose property is checked after a loop:

- `k` is chosen **strictly greater** than the maximum trip count (e.g. merge sort
  N=4 needs **k=5**, not 4 — k=4 is vacuous).
- Non-vacuity was confirmed by inserting a temporary `assert(0)` at the property
  site and checking ESBMC reports **VIOLATED** (reachable). If `assert(0)` passes,
  `k` is too small.
- `merge_sort.c` additionally bounds element values to `[0, 8)`: the permutation
  (multiset-equality) check over full-width `int` exhausts the bit-vector solver.
- `murmurhash.c` checks **memory safety only**, not determinism: proving two full
  hashes bit-identical is intractable for bounded bit-vector solving, while the
  out-of-bounds tail/block read is the real bug surface.

These verdicts are bounded — *verified up to k under esbmc 8.3.0* — not proofs for
all inputs. They are pinned against ESBMC output drift by
[`tests/esbmc/test_corpus.py`](../tests/esbmc/test_corpus.py).
