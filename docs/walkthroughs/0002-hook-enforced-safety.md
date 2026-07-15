# Walkthrough 0002 — One **hook-enforced** `write → verify → cex → fix` turn

Walkthrough [0001](./0001-manual-loop-abs.md) drove the loop *by hand*. This one is
driven by the **Claude Code adapter** ([`adapters/claude-code/`](../../adapters/claude-code/)):
the hooks are the gate, Claude is the worker, and the `forseti` CLI is the tool.
The distinction that matters:

> **Forseti does not loop.** Each `forseti verify` call returns one verdict and
> stops. The *harness* — here, a `PostToolUse` hook and a `Stop`-gate — owns the
> write → verify → fix loop and the "not done until VERIFIED" gate.

No harness function is written: ESBMC runs at the **function level**
(`--function my_abs`), havocs the parameter, and checks the built-in **safety**
properties. Outputs below are from esbmc 8.3.0; absolute paths are abbreviated.

---

## 0. Enable Forseti (once)

```console
$ pip install -e .            # puts `forseti` on PATH
$ # enable the adapters/claude-code plugin (or copy its hooks into .claude/settings.json)
$ claude                      # restart: hooks load at session start
```

## 1. Ask for the function

> **You:** Implement `int64_t my_abs(int64_t x)` that returns the absolute value, in `abs64.c`.

Claude writes the obvious version — a bare function, no `main`, no assertions:

```c
// abs64.c
#include <stdint.h>

int64_t my_abs(int64_t x) {
    return (x < 0) ? -x : x;
}
```

## 2. The PostToolUse hook verifies — automatically

The `Write` triggers the hook. It finds `my_abs`, runs
`forseti verify abs64.c --function my_abs --unwind 1 -- --overflow-check`, gets a
**VIOLATED** verdict, and feeds the counterexample back to Claude (hook exit 2):

```text
Forseti: 1 unit(s) did not verify (function-level ESBMC, safety properties).

✗ abs64.c::my_abs — VIOLATED (k=1)
Counterexample:
[Counterexample]

State 1 file abs64.c line 4 column 5 function my_abs thread 0
----------------------------------------------------
Violated property:
  file abs64.c line 4 column 5 function my_abs
  arithmetic overflow on neg
  CWE: CWE-190, CWE-191
  x < 0 => !overflow("unary-", x)

Fix the unit(s) to eliminate the counterexample; they will be re-verified
automatically on the next edit. Do not report the task done until every unit is
VERIFIED up to k. An UNKNOWN is not a pass — raise k (FORSETI_UNWIND) or simplify.
```

The gate records the verdict in `.forseti/gate_state.json` as `violated`. Note
what Claude never had to write: no harness, no `assert`, no nondet `main`. The
property here is **intrinsic** (no signed overflow), so ESBMC checks it for free.

## 3. If Claude tried to stop now — the Stop-gate blocks it

Were Claude to hand back the code while the unit is red, the `Stop` hook returns:

```json
{ "decision": "block",
  "reason": "Forseti verify-gate: 1 unit(s) are not VERIFIED up to k. Do not end
             the turn — fix them and let the gate re-verify, or explicitly report
             which unit / property / k could not be verified and why.\n\n
             ✗ abs64.c::my_abs — VIOLATED (k=1) ..." }
```

The turn cannot end. (After 3 consecutive blocks with no fix, the gate lets the
turn end but with a **loud** unverified residual — never a silent pass, never an
infinite loop.)

## 4. Fix — from the counterexample

The counterexample is the single point `x = INT64_MIN`, so the fix is targeted:

```c
// abs64.c
int64_t my_abs(int64_t x) {
    if (x == INT64_MIN) return INT64_MAX;
    return (x < 0) ? -x : x;
}
```

## 5. Re-verify (automatic) and close the turn

The `Edit` re-triggers the PostToolUse hook, which now reports:

```text
Forseti: VERIFIED up to k — abs64.c::my_abs (k=1)
```

The gate flips the unit to `verified` and resets the Stop-gate's patience. Claude
goes to stop; the `Stop` hook sees nothing outstanding and **approves** (exit 0).
The turn ends — and every unit it touched is verified up to k.

---

## Verdict

| Stage | Trigger | Verdict |
|---|---|---|
| write + verify | `PostToolUse` on `Write` | **VIOLATED** — `arithmetic overflow on neg`, `x = INT64_MIN` |
| stop attempt | `Stop` (while red) | **blocked** — turn cannot end |
| fix + re-verify | `PostToolUse` on `Edit` | **VERIFIED** up to k=1 |
| stop | `Stop` (green) | **approved** — turn ends |

The agent never handed back the broken draft. And Forseti stayed a stateless
oracle throughout — the loop lived entirely in the harness, exactly where it
belongs. Semantic properties (contracts beyond safety) are the **v1** step; see
[`adapters/claude-code/README.md`](../../adapters/claude-code/README.md).
