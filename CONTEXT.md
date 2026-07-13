# Forseti domain glossary

Names for the good seams in the code, so architecture reviews and tests speak one language.

- **Verification unit** — a function keyed `path::symbol`, the atom the loop and the store operate on.
- **Verdict** — ESBMC's answer for a unit + property: `VERIFIED` (up to bound *k*), `VIOLATED`
  (+ counterexample), `UNKNOWN` (inconclusive), or `ERROR` (tooling failure). Never a "proof".
- **k-escalation ladder** — the shared policy for turning an inconclusive `UNKNOWN` into a
  higher-bound re-verification: verify at each rung `(unwind, *unwind_ladder)`, escalating on
  `UNKNOWN`, settling on a terminal verdict once the ladder resolves or is exhausted — never a
  silent pass (CLAUDE.md, roadmap Risk 1). Owned by `orchestrator/ladder.py`
  (`validated_ladder` + `verify_ladder`) and consumed by **both** drivers, `run_loop`
  (`orchestrator/loop.py`) and `check_properties` (`orchestrator/check.py`), so the rule lives in
  exactly one place.
- **Loop driver** — `run_loop`: maps one source to one terminal `LoopState` and *fixes* on a
  violation.
- **Check driver** — `check_properties`: maps one unit + N properties to N per-property verdicts
  and does **no** fixing.
