# Architecture deepening backlog

Persisted candidate memory for `pm-deepen`. Statuses change; rows are never deleted.
`landed`/`dropped`/`rejected` rows are the memory that stops a recurring run re-deriving the
same ideas. Reconciled against `gh` at the start of every run.

## precond-reachability-probe-tri-state

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 4, blast radius 2, heat 4)
- **Files**: ~4–5 estimated
- **Modules**: `src/forseti/precond/verify.py` (`_assess_non_vacuity`), `src/forseti/precond/discharge.py` (`_check_caller`), a new `precond` interpreter leaf
- **Summary**: Give the duplicated assert(0)-reachability-probe interpretation (Violated+label → reached, Verified → unreachable, else → inconclusive) one tested home so its FAILED-means-reached inversion lives once beside its single emitter in `synth.py`.
- **First seen**: 2026-09-02

## hook-verdict-report-two-hooks

- **Status**: proposed
- **Score**: 19/25 (leverage 4, locality 4, blast radius 2, heat 3)
- **Files**: ~5 estimated
- **Modules**: `src/forseti/adapters/claude_code/post_tool_use.py`, `post_bash.py`, `stop_gate.py`
- **Summary**: Extract the near-verbatim `UnitVerdict[] → (events, message, exit code)` transform copied across the two PostToolUse hooks into one `verdict_report` module; scored as pure deepening (the `post_bash` canonical-event fix is excluded as a wire-format change).
- **First seen**: 2026-09-02
- **Reason**: runner-up candidate, within 1 point of the pick — natural next firing.

## counterexample-fired-label-predicate

- **Status**: proposed
- **Score**: 17/25 (leverage 3, locality 4, blast radius 2, heat 3)
- **Files**: ~3–4 estimated
- **Modules**: `src/forseti/esbmc/result.py` (`Violated`), `src/forseti/precond/verify.py`, `src/forseti/precond/discharge.py`
- **Summary**: Add a typed-first, raw-fallback label predicate on `Violated` and route the four precond raw-trace substring scans through it, so the label-matching convention lives with the typed result model.
- **First seen**: 2026-09-02
- **Reason**: not to be folded into `precond-reachability-probe-tri-state` — switching precond off the raw scan is a behaviour change and must be reviewed on its own.

## esbmc-caller-openings-module-split

- **Status**: proposed
- **Score**: 17/25 (leverage 3, locality 4, blast radius 3, heat 4)
- **Files**: ~4–6 estimated
- **Modules**: `src/forseti/esbmc/units.py`, `esbmc/__init__.py`, a new `esbmc` caller-openings module
- **Summary**: Move the ~460 LOC of discharge caller-openings analysis out of `units.py` (a module named for unit/signature listing) behind its existing `list_caller_openings`/`CallerOpenings` seam.
- **First seen**: 2026-09-02

## precond-under-unwound-detection-into-esbmc

- **Status**: proposed
- **Score**: 15/25 (leverage 3, locality 4, blast radius 4, heat 3)
- **Files**: ~3–4 estimated
- **Modules**: `src/forseti/precond/verify.py` (`_is_under_unwound`, `escalating_port`), `src/forseti/esbmc/result.py`/`runner.py`, `src/forseti/core/check.py`
- **Summary**: Fold under-unwound detection into `runner.classify`, beside the `UnknownReason.UNDER_UNWOUND` it already produces, retiring the precond raw-text `escalating_port` wrapper.
- **First seen**: 2026-09-02
- **Reason**: blast radius 4 — reaches out-of-scope `core/check.py` and changes `esbmc.verify`'s published verdict under one flag; wants a human's before/after differential. Eligible but deferred.

## core-store-session-boundary

- **Status**: proposed
- **Score**: 15/25 (leverage 3, locality 3, blast radius 2, heat 2)
- **Files**: ~4 estimated
- **Modules**: `src/forseti/core/check.py`, `core/propose.py`, `core/submit.py`
- **Summary**: Give the three Core faces one `store_session` context manager (owning the sqlite→`PropertyStoreError` translation) and one `read_unit` preamble helper, instead of three verbatim copies.
- **First seen**: 2026-09-02

## gate-env-config-extraction

- **Status**: proposed
- **Score**: 15/25 (leverage 2, locality 3, blast radius 2, heat 4)
- **Files**: ~3 estimated
- **Modules**: `src/forseti/adapters/claude_code/forseti_gate.py`, a new `env_config.py`
- **Summary**: Relocate the fail-closed env-parsing cluster out of the 2489-line `forseti_gate.py` into its own module (re-exported), the same move as the git-porcelain #205 extraction.
- **First seen**: 2026-09-02
- **Reason**: leverage 2 — a relocation, not a shallow→deep change; the interface (`gate.env_int(...)`) is unchanged.

## adapter-install-skeleton-two-harnesses

- **Status**: proposed
- **Score**: 14/25 (leverage 3, locality 3, blast radius 3, heat 2)
- **Files**: ~5 estimated
- **Modules**: `src/forseti/adapters/claude_code/install.py`, `adapters/codex/install.py`, a new `adapters/_install.py`
- **Summary**: Extract the shared idempotent managed-block install/remove skeleton (with the byte-identical outcome enums) so each adapter supplies only its merge/strip strategy and error type.
- **First seen**: 2026-09-02

## canonical-gate-decision-helper

- **Status**: proposed
- **Score**: 13/25 (leverage 2, locality 3, blast radius 2, heat 2)
- **Files**: ~4 estimated
- **Modules**: `src/forseti/core/events.py`, `adapters/claude_code/post_tool_use.py`, `adapters/codex/verify_hook.py`
- **Summary**: Move the canonical `gate.decision` event vocabulary into `core/events.py` beside its sibling `record_property_proposed`, preserving the deliberate `unit_ids` (Claude) vs `files` (Codex) field asymmetry.
- **First seen**: 2026-09-02
- **Reason**: overlaps the `hook-verdict-report-two-hooks` post_bash gap; low leverage.

## mcp-server-tool-wrappers

- **Status**: dropped
- **Score**: — (leverage 1)
- **Files**: n/a
- **Modules**: `src/forseti/core/mcp_server.py`
- **Summary**: The `*_tool` wrappers re-declare each `*_source` signature — but the typed, docstring'd param list *is* the MCP tool schema the SDK introspects.
- **First seen**: 2026-09-02
- **Reason**: Leverage 1 — deletion test scatters; shallow but load-bearing.

## verify-and-record-decomposition

- **Status**: dropped
- **Score**: — (not a deepening)
- **Files**: n/a
- **Modules**: `src/forseti/adapters/claude_code/forseti_gate.py` (`verify_and_record`, 1921-2489)
- **Summary**: The densest function in the tree, but it is the fail-closed/ownership heart of the gate — deep, not shallow.
- **First seen**: 2026-09-02
- **Reason**: Not a deepening candidate — high blast radius and high regression risk; every line encodes a fail-closed invariant.

## cli-run-handler-shape

- **Status**: dropped
- **Score**: — (leverage 1)
- **Files**: n/a
- **Modules**: `src/forseti/core/cli.py`
- **Summary**: The `try op() except: print; return 1` + json/human + exit-code shape repeats across ~6 handlers, but the exit-code logic genuinely differs per command.
- **First seen**: 2026-09-02
- **Reason**: Leverage 1 — consolidating would scatter the per-command exit-code variation into flags.

## esbmc-init-all-parser-surface

- **Status**: dropped
- **Score**: — (leverage 1)
- **Files**: n/a
- **Modules**: `src/forseti/esbmc/__init__.py`
- **Summary**: The package re-exports several `parse_*` scanners with no `src/` callers outside `units.py`; removing them from `__all__` narrows a namespace.
- **First seen**: 2026-09-02
- **Reason**: Leverage 1 — interface hygiene, not a deepening; nothing concentrates.

## harness-writer-port-inline

- **Status**: dropped
- **Score**: — (simplification, not a deepening)
- **Files**: n/a
- **Modules**: `src/forseti/orchestrator/check.py` (`SemanticHarnessWriter`), `orchestrator/ports.py` (`HarnessWriterPort`)
- **Summary**: A one-adapter seam whose `render` is a one-line delegation — a candidate to *inline*, not to deepen.
- **First seen**: 2026-09-02
- **Reason**: Not a deepening; the indirection defensibly keeps the driver from importing `properties` directly.
