<!--
Forseti — Codex adapter instructions.

Drop this file at your project root (or merge it into an existing AGENTS.md) so
Codex drives the Forseti write → verify → counterexample → fix loop. It requires
the Forseti Core registered as an MCP server named `forseti` — see
./config.toml.example and ./README.md.

The loop block below is a verbatim copy of adapters/prompt-tools-fallback.md
(#46); keep the two in sync.
-->

# Forseti verification loop (Codex)

This project uses **Forseti**: the ESBMC bounded model checker is available to
you as an MCP tool (`verify`) exposed by the `forseti` MCP server. A
`PostToolUse` hook already verifies your `apply_patch` edits and will **block**
with a counterexample when a unit is VIOLATED — so treat these instructions as
how to work *with* the gate: verify deliberately, read the counterexample it
returns, and fix. The hook cannot see edits made outside `apply_patch` (e.g. raw
shell writes), and an inconclusive `unknown` is surfaced rather than blocked, so
you remain responsible for the loop below.

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

## Semantic properties (functional correctness, not just safety)

The `PostToolUse` hook above only checks language-level *safety* (memory
safety, overflow, bounds, UB) on the whole edited file — it says nothing
about whether a function computes the right thing. For a unit whose
correctness matters beyond memory safety, use the `semantic_loop` MCP tool
(issue #213) to check a *semantic* property you have in mind for it — "the
output is sorted", "abs(x) >= 0", "the result is a permutation of the input"
— something ESBMC cannot infer on its own. Invoke this deliberately, per
unit, after a function whose contract matters; it is not run on every edit.

1. **Submit your own candidate properties, then check.** You already read
   and wrote the code, so state the property yourself rather than asking a
   nested LLM to guess one — call the `semantic_loop` MCP tool with `mode:
   "submit"`, the unit's `source`/`function`, and `candidates`: a list of
   `{"expression": ..., "domain": [...], "referenced_params": [...],
   "rationale": ...}` objects (`expression` is a C boolean over the unit's
   parameters and `result`; `domain` states any precondition the property
   assumes, e.g. `"x > INT64_MIN"`). Set `provider`/`model` to identify
   yourself (e.g. `"codex"` / your own model name) — Core records this as
   the property's provenance, never guessed. Each candidate is statically
   validated and, if accepted, persisted to the project's
   `.forseti/forseti.db` store, then immediately rendered into a
   self-contained ESBMC harness and verified — escalating a
   loop-under-unwound `unknown` along a k-ladder before settling. To
   re-check without re-submitting (e.g. after a fix), call again with
   `mode: "check_only"` and no `candidates`.

2. **Read `outcome`, not just the call's success or failure.** The result's
   top-level `outcome` is Core's own worst-outcome-wins policy across every
   property this call checked: `held` (every checked property held, up to
   its settled `k`), `violated` (at least one has a counterexample),
   `unknown` (inconclusive — never a pass), `error` (a tooling failure), or
   `empty` (nothing was checkable — every candidate was rejected, or none
   was submitted). Per-property detail is under `check.verdicts[]` — each
   has `property_id`, `outcome`, `k`, and (for a settled verdict) `result`;
   a `violated` one carries `result.raw_counterexample` (ESBMC's trace
   text). `ingestion[]` carries each submitted candidate's own accept/reject
   result, so a rejected candidate stays visible, never silently dropped.

3. **Act on it before you hand the turn back.** For `violated`, read the
   counterexample, fix the unit so the property holds, then re-check with
   `mode: "check_only"`. For `unknown`, raise `unwind`/`unwind_ladder` or
   simplify the unit; never treat it as done. For `empty`, say so explicitly
   — it is not the same as `held`. Do not declare the unit's semantic
   correctness settled until every property you submitted for it reads
   `held`.

The safety verify hook above still gates every `apply_patch` edit
independently; this loop is the semantic half you drive yourself over MCP —
the same Core operation and the same `outcome` field the Claude Code
adapter's `forseti-property-check` subagent uses
(`adapters/claude-code/agents/property-check.md`), just triggered
differently: there, an explicitly-invoked Bash subagent; here, a tool call
you make directly, since Codex has no subagent concept of its own.
