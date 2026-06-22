# Walkthrough 0001 — One manual `write → verify → counterexample → fix` turn

This is the one **manual** loop turn required by the P0 exit criterion
([`docs/roadmap.md`](../roadmap.md): *"one manual write→verify→cex→fix turn completed by hand on a
C kernel"*), driven by hand with the `forseti-esbmc` wrapper on the classic `abs`/`INT64_MIN` bug.
Nothing here is automated — automating this spine is P1. The point is to prove the skeleton end to
end before we build the loop on top of it.

The unit and its fix already live in the repo: [`examples/abs.c`](../../examples/abs.c) (buggy) and
[`examples/abs_fixed.c`](../../examples/abs_fixed.c) (fixed). Every command below was run against
**esbmc 8.3.0**; the transcripts are pasted verbatim.

---

## 1. Write — the unit and its harness

`my_abs` looks correct, and a harness asserts the property we care about (`my_abs(x) >= 0` for every
`int64_t`). `x` is left nondeterministic so ESBMC explores the whole input space, not one example.

```c
// examples/abs.c
int64_t my_abs(int64_t x) {
    return (x < 0) ? -x : x;
}

int main(void) {
    int64_t x = nondet_int64();
    assert(my_abs(x) >= 0);
    return 0;
}
```

## 2. Verify — ask ESBMC for a verdict

We run the wrapper at its default bound (`k=1`); `verify()` invokes esbmc with
`--unwind 1 --no-unwinding-assertions` and, deliberately, **no** `--overflow-check`.

```console
$ uv run forseti-esbmc examples/abs.c
VIOLATED  (examples/abs.c, k=1, esbmc 8.3.0)

[Counterexample]


State 1 file examples/abs.c line 14 column 5 function main thread 0
----------------------------------------------------
  x = -9223372036854775807 - 1 (10000000 00000000 00000000 00000000 00000000 00000000 00000000 00000000)

State 2 file examples/abs.c line 15 column 5 function main thread 0
----------------------------------------------------
Violated property:
  file examples/abs.c line 15 column 5 function main
  assertion my_abs(x) >= 0
  return_value$_my_abs$2 >= 0
$ echo $?
1
```

The verdict is **VIOLATED** — not a "proof of a bug," but a concrete, reproducible counterexample.
The CLI exit code `1` is its own contract (`VERIFIED 0 | VIOLATED 1 | UNKNOWN 2 | ERROR 3`).

## 3. Read the counterexample

ESBMC hands back the exact input that breaks the property:

- **Input** (State 1): `x = -9223372036854775807 - 1`. That is **INT64_MIN** — esbmc 8.3.0 prints it
  as `-9223372036854775807 - 1` rather than the unrepresentable positive literal
  `9223372036854775808`; the binary `10000000 …` (top bit set, 63 zeros) confirms it.
- **Violated property** (State 2): our own `assertion my_abs(x) >= 0`.

Why it fails: for `x = INT64_MIN`, the `x < 0` branch computes `-x`. But `-INT64_MIN` is not
representable in `int64_t`, so under two's-complement wraparound it stays `INT64_MIN` — still
negative. `my_abs(INT64_MIN)` therefore returns a negative value and the assertion fails.

> The violated property is the **user assertion** precisely because the run used no
> `--overflow-check`. With that flag, esbmc would instead stop earlier on `arithmetic overflow on
> neg` — a different property. This turn deliberately checks *our* property, the one the harness
> states, which is what the loop will do for hand-written and (later) generated properties.

## 4. Fix — widen the one bad input

The counterexample is a single point, so the fix is targeted: saturate `INT64_MIN` to `INT64_MAX`
instead of negating it. Every other input already takes a representable path.

```c
// examples/abs_fixed.c
int64_t my_abs(int64_t x) {
    if (x == INT64_MIN) return INT64_MAX;
    return (x < 0) ? -x : x;
}
```

## 5. Re-verify — close the turn

```console
$ uv run forseti-esbmc examples/abs_fixed.c
VERIFIED  (examples/abs_fixed.c, k=1, esbmc 8.3.0)
$ echo $?
0
```

**VERIFIED up to k=1.** ESBMC found no input (including the former counterexample `INT64_MIN`) that
violates `my_abs(x) >= 0`. This is a bounded, reproducible verdict — *verified up to k under esbmc
8.3.0* — not a kernel-checkable proof for all inputs.

---

## Verdict

One `write → verify → counterexample → fix` turn, completed by hand:

| Stage | Artifact | Verdict |
|---|---|---|
| write + verify | `examples/abs.c` | **VIOLATED** — counterexample `x = INT64_MIN`, exit 1 |
| fix + re-verify | `examples/abs_fixed.c` | **VERIFIED** up to k=1, exit 0 |

This satisfies the manual-turn leg of the P0 exit criterion ([`docs/roadmap.md`](../roadmap.md)).
The turn is pinned against ESBMC output drift by `tests/esbmc/test_verify_integration.py`.
