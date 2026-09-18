# Demo workspace: write -> verify -> fix

Write the program requested in this workspace.

Forseti's Claude Code verify-gate hooks (SessionStart / PostToolUse / Stop)
are already installed here. On every edit to a C file they automatically run
ESBMC's function-level safety check (memory safety, array bounds, signed
overflow, division by zero, UB) and record a verdict per function. A
blocking verdict (`violated` / `unknown` / `error`) blocks the turn from
ending; `verified` passes and never blocks.

## Pointer/array parameters: `needs_contract`

A function taking a pointer or array parameter cannot be given a real
verdict by the function-level hook alone (ESBMC has no valid object to
point it at), so the hook reports it as `needs_contract` instead.
**`needs_contract` is non-blocking and purely informational** — it is not
evidence the function is safe, and the Stop hook will not fail the turn
over it by itself.

For every function taking a pointer or array parameter, you must
additionally, yourself:

1. Run `forseti synth --function <name> <file> --json` against it (run
   `forseti synth --help` if you need other flags, e.g. `--max-len` or
   `--timeout`). Always pass `--json`: without it, `synth` only prints a
   human-readable label with no `"assessment"` field, and the demo's
   observability pane (`render.py`/`canvas/`) can't extract or color-code
   the verdict from that output.
2. Read the verdict: `assumed_verified`, `discharged_verified`, `violated`,
   `vacuous`, `unknown`, `needs_contract`, or `error`.
3. If the verdict is `violated`, fix the function's implementation and
   re-run `forseti synth` on it. Repeat until the verdict is
   `assumed_verified` or `discharged_verified`.
4. If the verdict is `vacuous`, that is **not a pass** — it means the
   precondition made the call site unreachable, so nothing was actually
   exercised. Loosen the harness (e.g. raise `--max-len`) or check for an
   overly-strong caller assumption, then re-run; treat it like `unknown`
   below and never move on as if it were `assumed_verified`.

Never consider the task done while any pointer/array-taking function still
has a `violated` synth verdict. A `needs_contract` verdict from the
PostToolUse/Stop hooks does not excuse this check — it is exactly the
signal that a manual `forseti synth` run is required.

**`unknown` is not a pass, ever.** If a re-run reports `unknown`, that is
not "not violated" — it means ESBMC could not settle the question up to the
bound it tried. Raise `--timeout` or `--max-len`, or simplify the function,
and try again; if it still won't settle, report it as an unresolved unit —
never treat `unknown` as done and never silently move on.

## Semantic properties

Once a pointer/array-taking function reaches `assumed_verified` or
`discharged_verified` (memory-safe under `forseti synth`), also run:

```
forseti semantic-loop <file> --function <name> --mode propose --json
```

This asks an LLM to propose candidate semantic properties about the
function (e.g. relationships between its inputs, its return value, and any
output parameters), then checks each one with ESBMC — all in one command.

Read the top-level `outcome`:

- `held` — every proposed property was checked and holds up to the bound
  tried (still bounded, not a proof — same discipline as below).
- `violated` — at least one proposed property failed; look at
  `check.verdicts[]` for which one and its counterexample. A violated
  *semantic* property is different from a memory-safety bug: the property
  was the model's own guess about the function's contract, so it may simply
  be a wrong guess rather than a real defect. Use judgment — if the
  counterexample points at a genuine behavioral problem, consider fixing it;
  if the property itself looks like an incorrect assumption, say so plainly
  rather than "fixing" the code to satisfy an arbitrary guess. Never fix
  silently and never ignore silently — report which it was.
- `unknown` — at least one property could not be settled. Not a pass, same
  as the safety gate's own `unknown`.
- `error` — a tooling failure (report it, don't retry blindly).
- `empty` — no checkable properties were proposed or survived validation;
  this is not evidence of anything about the function.

## Vocabulary

ESBMC produces a **verdict**, not a proof. Report results as `VERIFIED up
to k`, `VIOLATED`, or `UNKNOWN` — never "proven" or "correct". The same
discipline applies to semantic-property outcomes above: `held` means
"holds up to the bound checked," not "proven."
