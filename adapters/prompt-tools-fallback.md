# Prompt+tools fallback — the reusable loop instructions

This is the **harness-neutral prompt** that drives the Forseti loop where a
harness has **no tool-use hooks** to force verification (opencode; Codex in part
— see [RFC-0001](../docs/design/0001-harness-portability.md)). Instead of a hook
running `verify` and a `Stop`-gate blocking "done", the *agent itself* is told to
call the Core and to keep fixing until the unit passes. **Same Core, weaker
enforcement** — the gate is only as strong as the model's adherence to these
instructions.

Each per-harness adapter (`adapters/codex/`, `adapters/opencode/`) embeds or
references the block below **verbatim**, so the instructions have one source of
truth. It assumes the Forseti Core is reachable as an **MCP server** (`forseti
mcp`, from #49) exposing a `verify` tool; register that server per the adapter's
instructions before using this prompt.

---

<!-- BEGIN forseti-fallback-instructions -->
## Verify before you hand code back

You are editing code with the **Forseti** verifier available as an MCP tool.
Forseti runs the ESBMC bounded model checker and returns a **verdict** — never a
"proof". Follow this loop for every code **unit** (a function, keyed
`path::symbol`) you write or change:

1. **After editing a unit, call the `verify` tool on its source file.** Pass the
   file `source`, and set `unwind` (the loop bound *k*) and `function` when you
   want to scope the check. Do not announce the work finished before this call.

2. **Act on the verdict in the returned JSON payload:**
   - **`verified`** — no violation was found **up to bound k**. Treat this as
     "verified up to k", *not* a general proof. You may proceed.
   - **`violated`** — the payload carries a concrete **`counterexample`** (a
     failing input and path). Read it, change the unit to eliminate that failure,
     then verify again. Repeat until it is no longer violated.
   - **`unknown`** — the check was **inconclusive** (timeout, or k too small; see
     `reason`). This is **not** a pass. Do one of: raise `unwind` (k) and
     re-verify; simplify the harness/unit so the check terminates; or, if neither
     works, **report the residual to the human**. Never treat `unknown` as done.
   - **`error`** — the verifier could not run (see `message`). Fix the inputs or
     invocation and retry; do not proceed as if it passed.

3. **Emulated Stop-gate.** Do not declare the task complete, and do not hand the
   code back, until every changed unit is **`verified` up to the agreed k**. If a
   unit cannot be made to verify, say so explicitly — which unit, which property,
   at what k, and why — instead of quietly moving on. An unverified or `unknown`
   unit is a blocker to report, never a silent pass.

Keep the write → verify → counterexample → fix cycle tight: verify the smallest
unit you just touched, fix from the counterexample, and re-verify, rather than
batching many edits before a single check.
<!-- END forseti-fallback-instructions -->

---

## Why it reads the way it does

- **"Verdict, not proof" and "verified up to k"** keep the vocabulary honest:
  ESBMC finds no violation *within bound k*, which is weaker than a general
  guarantee (see RFC-0001, "What ESBMC actually returns").
- **`unknown` is a distinct, non-passing state.** Silently accepting a timeout or
  an under-unwound loop would defeat the point of the gate; the fallback must
  carry the same UNKNOWN discipline the hooked path does.
- **The Stop-gate is emulated in prose** because there is no hook to enforce it.
  That is the explicit trade the fallback makes; the [contract](./README.md)
  spells out what this does and does not guarantee.
