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
you as an MCP tool (`verify`) exposed by the `forseti` MCP server. Codex has no
tool-use hook that can *force* verification, so enforcement is the
**prompt+tools fallback**: you are responsible for running the loop below. The
`notify` hook (see README) is only a post-turn signal, not a gate.

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
