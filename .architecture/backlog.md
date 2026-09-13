# Architecture deepening backlog

Persisted candidate memory for `pm-deepen`. Statuses change; rows are never deleted.
`landed`/`dropped`/`rejected` rows are the memory that stops a recurring run re-deriving the
same ideas. Reconciled against `gh` at the start of every run.

## check-source-ladder-default

- **Status**: landed
- **Score**: 20/25 (leverage 4, locality 4, blast radius 2, heat 4)
- **Files**: ~4 estimated
- **Modules**: `src/forseti/core/check.py` (`check_source`, `default_unwind_ladder_above`), `src/forseti/core/cli.py` (`_run_check`), `src/forseti/core/mcp_server.py` (`check_tool`), `src/forseti/core/loop.py` (`run_semantic_loop`)
- **Summary**: Make `check_source` own its unwind-ladder default (`unwind_ladder=None → default_unwind_ladder_above(unwind)` internally) so its three callers stop repeating the `None → derive` branch and a direct `check_source(unwind=8)` stops raising on the `(8,8,16)` collision. Pure deepening, no wire change: the CLI/MCP boundaries already default to the derived ladder.
- **First seen**: 2026-09-04
- **PR**: #267
- **Reason**: picked its run (top score, 20/25); within 1 point of the perennial runner-up `hook-verdict-report-two-hooks` (19/25), taken on the deterministic tie-break (heat) and on being pinnable esbmc-free.

### Run 2026-09-04 — complete

- **Outcome**: complete
- **Stopped at**: step 6 — PR opened; work landed on branch
- **Branch**: `pm-deepen/check-source-ladder-default` — *created* as `pm-deepen/run-2026-09-04-0102` from `origin/main` and renamed at step 2. Branch adoption was **refused** at step 0 on condition 3: the firing branch (`sym/forseti/routine/refactor-audit/01M1MR2N0S`) had an upstream (`@{u}` resolved to `origin/main`), so it was not a made-for-this-run, no-upstream branch.
- **Committed**: review report + design pass, the `refactor(core)` implementation (check_source owns its ladder default; three callers forward through; new `test_core_check.py`/`test_core_loop.py` pins), and this backlog update.
- **Evidence**: quality gate green — ruff check + ruff format --check + ty check + pytest (1552 passed, 1 skipped, ESBMC-gated included); project coverage 97.81% (gate 96%); PR #267.
- **Next**: human review of PR #267 (do not merge as part of the routine). Natural next firing: the perennial runner-up `hook-verdict-report-two-hooks` (19/25) — but add an esbmc-free characterization test for `post_bash._report` first (pinned only behind `@skipif(not _HAVE_ESBMC)` today).

### Run 2026-09-11 — reconciled

- **Outcome**: reconciled `in-flight` → `landed`. PR #267 merged 2026-09-04T08:11Z (`gh pr view 267 --json state,mergedAt`).

## precond-reachability-probe-tri-state

- **Status**: landed
- **Score**: 20/25 (leverage 4, locality 4, blast radius 2, heat 4)
- **Files**: ~4–5 estimated
- **Modules**: `src/forseti/precond/verify.py` (`_assess_non_vacuity`), `src/forseti/precond/discharge.py` (`_check_caller`), new leaf `src/forseti/precond/reachability.py`
- **Summary**: Give the duplicated assert(0)-reachability-probe interpretation (Violated+label → reached, Verified → unreachable, else → inconclusive) one tested home so its FAILED-means-reached inversion lives once beside its single emitter in `synth.py`.
- **First seen**: 2026-09-02
- **PR**: #254

### Run 2026-09-02 — complete

- **Outcome**: complete
- **Stopped at**: step 6 — PR opened; work landed on branch
- **Branch**: `pm-deepen/precond-reachability-probe-tri-state` — *created* as `pm-deepen/run-2026-09-02-0102` from `origin/main` and renamed at step 2. Branch adoption was **refused** at step 0 on condition 3: the firing branch's `@{u}` resolved to `origin/main` (it has an upstream), so it was not a made-for-this-run, no-upstream branch.
- **Committed**: review report, design pass, the `refactor(precond)` implementation, and this backlog update.
- **Evidence**: quality gate green — ruff check + ruff format --check + ty check + pytest (1494 passed, 1 skipped, ESBMC-gated included); project coverage 97.40% (gate 96%); PR #254.
- **Next**: human review of PR #254. Natural next firing: the runner-up candidate `hook-verdict-report-two-hooks` (19/25, within 1 point), taken as pure extraction with its wire-format `gate.decision` gap filed as `canonical-gate-decision-helper`.

## propose-submit-ingest-trace-seam

- **Status**: landed
- **Score**: 20/25 (leverage 4, locality 4, blast radius 2, heat 4)
- **Files**: ~6 estimated
- **Modules**: `src/forseti/core/propose.py` (`propose_source`), `src/forseti/core/submit.py` (`submit_source`), `src/forseti/core/check.py` (`check_source`, store-open site), new leaf `src/forseti/core/persistence.py`
- **Summary**: Give the property-ingest persistence boundary one home — the `sqlite3.Error → PropertyStoreError` translation shared by propose/submit/check, plus the `persist=False` dry-run invariant and `record_property_proposed` trace dispatch shared by propose/submit — so the two proposer faces collapse to "read unit → build request → delegate" instead of each carrying a byte-identical epilogue held in lockstep by copied comments.
- **First seen**: 2026-09-03
- **PR**: #262
- **Reason**: fresh candidate in the #252 semantic-loop churn; `submit.py` post-dates the prior firing's scan. Picked this run (top score); within 1 point of `hook-verdict-report-two-hooks`.

### Run 2026-09-03 — complete

- **Outcome**: complete
- **Stopped at**: step 6 — PR opened; work landed on branch
- **Branch**: `pm-deepen/propose-submit-ingest-trace-seam` — *created* as `pm-deepen/run-2026-09-03-0102` from `origin/main` and renamed at step 2. Branch adoption was **refused** at step 0 on condition 3: the firing branch's `@{u}` resolved to `origin/main` (it has an upstream), so it was not a made-for-this-run, no-upstream branch.
- **Committed**: review report + design pass, the `refactor(core)` implementation (new `core/persistence.py` seam + `test_core_persistence.py`, propose/submit/check rewired to delegate), and this backlog update.
- **Evidence**: quality gate green — ruff check + ruff format --check + ty check + pytest (1547 passed, 1 skipped, ESBMC-gated included); project coverage 97.82% (gate 96%), `core/persistence.py` 100%; PR #262.
- **Next**: human review of PR #262 (do not merge as part of the routine). Natural next firing: the runner-up candidate `hook-verdict-report-two-hooks` (19/25, within 1 point), a pure extraction of the duplicated `UnitVerdict[] → report` transform across the two PostToolUse hooks.

### Run 2026-09-04 — reconciled

- **Outcome**: reconciled `in-flight` → `landed`. PR #262 merged 2026-09-03 (`gh pr view 262`); confirmed on `origin/main` as `5625201`.

## hook-verdict-report-two-hooks

- **Status**: landed
- **PR**: #275
- **Score**: 19/25 (leverage 4, locality 4, blast radius 2, heat 3)
- **Files**: ~5 estimated
- **Modules**: `src/forseti/adapters/claude_code/post_tool_use.py`, `post_bash.py`, `stop_gate.py`
- **Summary**: Extract the near-verbatim `UnitVerdict[] → (events, message, exit code)` transform copied across the two PostToolUse hooks into one `verdict_report` module; scored as pure deepening (the `post_bash` canonical-event fix is excluded as a wire-format change).
- **First seen**: 2026-09-02
- **Reason**: **picked 2026-09-11** — top-scoring eligible candidate (19/25) now that PR #267 cleared the in-flight slot; the fourth firing it surfaced, first it was taken. Ties the fresh `unit-id-value-type` (19/25) but wins the tie-break on lower blast radius (2 < 3). Scope: the two full sites (`post_tool_use` inline + `post_bash._report`) only; `stop_gate._residual` (a `dict`-shaped partial, own `_CEX_CLIP=1200` vs the hooks' `CEX_CLIP=1500`) is out of scope — different shape, folding it in would be a wire/shape change. Test-first precondition (satisfied this run): add esbmc-free pins for `post_bash._report`'s failure branch, today reachable only behind `@skipif(not _HAVE_ESBMC)` (`test_out_of_band.py`).

### Run 2026-09-11 — complete

- **Outcome**: complete
- **Stopped at**: step 6 — PR #275 opened; work landed on branch
- **Branch**: `pm-deepen/hook-verdict-report-two-hooks` — *created* as `pm-deepen/run-2026-09-11-0104` from `origin/main` and renamed at step 2. Branch adoption was **refused** at step 0 on condition 3: the firing branch (`sym/forseti/routine/refactor-audit/01M26RZFXK`) had an upstream (`@{u}` resolved to `origin/main`), so it was not a made-for-this-run, no-upstream branch.
- **Committed**: review report + design pass (winner: config-as-data / style object; runner-up design C lost on locality), the ESBMC-free byte-for-byte oracle (`test_out_of_band`/`test_post_tool_use`), the `refactor(claude_code)` implementation (new `verdict_report.py` seam: `ReportStyle`/`partition`/`render`/`emit`; both hooks rewired; new `test_verdict_report.py`), and this backlog update.
- **Evidence**: quality gate green — ruff check + ruff format --check + ty check + pytest (1650 passed, 1 skipped, ESBMC-gated included); project coverage 97.98% (gate 96%); `verdict_report.py`/`post_tool_use.py`/`post_bash.py` all 100%; PR #275. Diff 6 files vs ~5 estimate (within the 2× bail threshold); no public/wire interface touched; `stop_gate` untouched.
- **Next**: human review of PR #275 (do not merge as part of the routine). Natural next firing: the runner-up candidate `unit-id-value-type` (19/25, tied, lost the blast-radius tie-break) — pick a scope (broad UnitId type vs its narrow `proposal-request-prologue` subset) before implementing so the two don't collide.

### Run 2026-09-14 — reconciled

- **Outcome**: reconciled `in-flight` → `landed`. PR #275 merged 2026-09-11T09:13:46Z (`gh pr view 275 --json state,mergedAt`); confirmed on `origin/main` as HEAD `e7f8874`.

## unit-id-value-type

- **Status**: proposed
- **Score**: 19/25 (leverage 4, locality 4, blast radius 3, heat 4)
- **Files**: ~10 estimated
- **Modules**: construction at `core/propose.py:70`, `core/submit.py:77`, `orchestrator/ports.py:107`, `adapters/claude_code/property_gate.py:234`, `adapters/claude_code/forseti_gate.py:1662,1832,1912,2055,2254`, `adapters/oh_my_pi/verify_hook.py:239`; inverse parse `properties/proposer.py:87`; a new `UnitId`/`make_unit_id`/`unit_id_symbol` seam (`properties/unit_id.py`)
- **Summary**: The `path::symbol` unit-id convention has no owning module — ~10 sites hand-format it and one consumer (`proposer.py:87`) re-parses it with a silent malformed-id fallback, so producers and consumer can drift with no enforcement. Give it one constructor/parser with a pinned round-trip. Scope is the `::` **join + split** only; each caller keeps its current left-operand path spelling, so every produced string and DB key is byte-identical.
- **First seen**: 2026-09-11
- **Reason**: **picked 2026-09-14** — top-scoring eligible candidate (19/25) now that PR #275 landed and cleared the in-flight slot. Within 1 point of the runner-up candidate `proposal-request-prologue` (18/25), which overlaps only at the two Core producer lines (`propose.py:70`, `submit.py:77`). Scope settled at **join+split concentration, no normalization change**: the Core(raw `source`)-vs-gate(`..`-resolved rel via `forseti_gate.unit_id()`) left-operand asymmetry (`property_gate.py:44-55`) is a *deliberate, documented* residual — unifying it is a behaviour/wire change, out of scope for an unattended run and reported for a human. The consumer-side slug derivations (`orchestrator/persistence.py`, `orchestrator/check.py`) are filed separately as `unit-id-slug-derivations`, not folded in.

## proposalresult-provenance-reflatten

- **Status**: proposed
- **Score**: 17/25 (leverage 3, locality 4, blast radius 2, heat 4)
- **Files**: ~3–4 estimated
- **Modules**: `properties/proposer.py:125-152` (`ProposalResult`), `:252-277` (`propose_properties`), `:311-337` (`submit_candidates`); docstring fix `core/propose.py:13-16`
- **Summary**: `ProposalResult` re-declares the exact four fields of `Provenance` (`model.py:52-66`) and both proposer faces restate them when building result + provenance. Carry a `Provenance` behind `@property` shims (keeping the flat #44 `to_dict` wire shape and the `result.provider`/`result.model` readers) instead of restating. Folds in a live docstring drift: `core/propose.py:13-16` claims it "does not wire the #64 renderability gate" but it does (`propose.py:73` → `proposer.py:413`).
- **First seen**: 2026-09-11

## store-column-registry

- **Status**: proposed
- **Score**: 15/25 (leverage 2, locality 4, blast radius 2, heat 3)
- **Files**: ~2 estimated
- **Modules**: `properties/store.py:29-143` (`_SCHEMA`, `_INSERT`, `_MIGRATED_COLUMNS`, `_property_to_row`, `_row_to_property`), `tests/properties/test_store.py`
- **Summary**: The 14-column property shape is restated five times; adding one field is a five-site lockstep edit. Derive schema/insert/migration/both mappers from one column registry.
- **First seen**: 2026-09-11
- **Reason**: leverage bounded — locality is already good (all five sites in one file), so this concentrates a lockstep edit but does not turn a shallow module deep. Nice-to-have.

## signature-reexport-indirection

- **Status**: dropped
- **Score**: — (leverage 1)
- **Files**: n/a
- **Modules**: `properties/__init__.py:14-28`, `properties/harness.py:29-36`, `properties/signature.py`, `properties/proposer.py:34-39`
- **Summary**: `UnitSignature`/`BufferParam`/`ScalarParam`/`Param`/`HarnessError`/`extract_signature` are defined in `signature.py` but imported "from `.harness`", so finding where they live is a two-hop bounce. Redirect the six names to `from .signature import ...`.
- **First seen**: 2026-09-11
- **Reason**: Leverage 1 — `__init__.py` is a facade for ~10 external importers (deleting it scatters), and redirecting six names is interface hygiene, not a shallow→deep change; nothing concentrates.

## proposal-request-prologue

- **Status**: proposed
- **Score**: 18/25 (leverage 3, locality 4, blast radius 2, heat 4)
- **Files**: ~3 estimated
- **Modules**: `src/forseti/core/propose.py` (`propose_source`), `src/forseti/core/submit.py` (`submit_source`), a new/`persistence.py` builder
- **Summary**: Extract the verbatim read-unit→`ProposalRequest` prologue (`read_text` → `unit_id` → best-effort `extract_signature` degrade → build request) shared by propose/submit into one `build_proposal_request` helper — the *head* complement of the store-open/dry-run/trace *tail* that PR #262 already absorbed into `core/persistence.py`.
- **First seen**: 2026-09-04
- **Reason**: absorbs the `read_unit`-preamble half of the now-superseded `core-store-session-boundary`.

## counterexample-fired-label-predicate

- **Status**: proposed
- **Score**: 17/25 (leverage 3, locality 4, blast radius 2, heat 3)
- **Files**: ~3–4 estimated
- **Modules**: `src/forseti/esbmc/result.py` (`Violated`), `src/forseti/precond/verify.py`, `src/forseti/precond/discharge.py`
- **Summary**: Add a typed-first, raw-fallback label predicate on `Violated` and route the precond raw-trace substring scans through it, so the label-matching convention lives with the typed result model.
- **First seen**: 2026-09-02
- **Reason**: not to be folded into `precond-reachability-probe-tri-state` — switching precond off the raw scan is a behaviour change and must be reviewed on its own. Re-scan 2026-09-14: **three** direct raw scans, not four (`verify.py:145`, `discharge.py:640`, `reachability.py:55`; the earlier "4" counted `classify_site_probe`'s two call sites, which already route through `reachability.py:55`). Deletion-test caveat: `esbmc/counterexample.py`'s `ViolatedProperty` does not capture the ESBMC assert label, so a `Violated.fired(label)` predicate still substring-scans internally today — it concentrates the *convention* and gives one future upgrade point, rather than being a pure move. Score held at 17/25.

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

- **Status**: landed
- **Score**: 15/25 (leverage 3, locality 3, blast radius 2, heat 2)
- **Files**: ~4 estimated
- **Modules**: `src/forseti/core/check.py`, `core/propose.py`, `core/submit.py`
- **Summary**: Give the three Core faces one `store_session` context manager (owning the sqlite→`PropertyStoreError` translation) and one `read_unit` preamble helper, instead of three verbatim copies.
- **First seen**: 2026-09-02
- **Reason**: resolved incidentally — PR #262 landed the `store_session` half as `open_store` in `core/persistence.py` (the context manager owning the sqlite→`PropertyStoreError` translation, used by check/propose/submit). The `read_unit`-preamble half is re-filed fresh as `proposal-request-prologue` (18/25) with a build-request scope.

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
- **Score**: 17/25 (leverage 3, locality 3, blast radius 2, heat 4)
- **Files**: ~4 estimated
- **Modules**: `src/forseti/core/events.py`, `adapters/claude_code/post_tool_use.py` (`_record_gate_decision`), `adapters/codex/verify_hook.py` (`_record_gate_decision`), `adapters/oh_my_pi/verify_hook.py` (`_record_gate_decision`), `adapters/claude_code/stop_gate.py` (`_record_semantic_gate_decision`)
- **Summary**: Move the canonical `gate.decision` event emitter into `core/events.py` beside its sibling `record_property_proposed`, as one `record_gate_decision(store_root, *, harness, adapter, decision, unit_ids=None, files=None, file=None)`, preserving the deliberate `unit_ids` (Claude/Oh-My-Pi) vs `files` (Codex) field asymmetry.
- **First seen**: 2026-09-02
- **Reason**: **now four sites, not three** (re-scan 2026-09-14): `post_tool_use.py:43-55`, `codex/verify_hook.py:184-207`, `oh_my_pi/verify_hook.py:294-311` (**missed by the prior scan**), and `stop_gate._record_semantic_gate_decision:231-249` (docstring at `:236` says "Mirrors `post_tool_use._record_gate_decision`"). Four emit sites across three harnesses in hot code; the sibling slot `record_property_proposed` already sits in `events.py:65-84`. Runner-up-tier at 17/25 (leverage held at 3 — a helper must still carry the store-root and payload-key divergences), the natural firing after `unit-id-value-type`. Could fold in as the emit sub-seam of the (now landed) `hook-verdict-report-two-hooks`.

## hook-stdin-envconfig-prologue

- **Status**: proposed
- **Score**: 16/25 (leverage 3, locality 3, blast radius 3, heat 4)
- **Files**: ~5 estimated
- **Modules**: `adapters/claude_code/post_tool_use.py`, `post_bash.py`, `stop_gate.py`, `session_start.py`, `adapters/codex/verify_hook.py`
- **Summary**: Collapse the copied `read stdin → json` decode (4 Claude hooks + a divergent Codex 5th) into one `read_hook_input() → dict` seam, forcing one deliberate crash-vs-swallow decision. The stacked env-config fail-closed block only half-concentrates (detection collapses, emit stays per-harness), so overlaps `gate-env-config-extraction`.
- **First seen**: 2026-09-04
- **Reason**: partial deletion test — the env-config half's emit is harness-specific; take the stdin-decode half as the clean sub-seam.

## unit-id-slug-derivations

- **Status**: proposed
- **Score**: 18/25 (leverage 3, locality 4, blast radius 1, heat 3)
- **Files**: ~2-3 estimated
- **Modules**: `orchestrator/persistence.py:30-39` (`_unit_slug`), `orchestrator/check.py:417-426` (`_harness_filename`)
- **Summary**: Two independent, non-identical sanitizations turn the same `path::symbol` key into a filesystem-safe name — `_unit_slug` does `.replace("::", "__").replace("/", "_")` + a hash suffix; `_harness_filename` does `re.sub(r"[^A-Za-z0-9_.-]", "_", unit_id)`. The **consumer** complement of `unit-id-value-type`: once a `UnitId` type exists, a single `UnitId.slug()` concentrates both rules.
- **First seen**: 2026-09-14
- **Reason**: fresh this run from the exploration's consumer-side sweep. Deliberately **not** folded into the `unit-id-value-type` pick — that pick is scoped to `::` join+split only; extending it to slugging would inflate its blast radius past the score it was picked on. Natural firing after the `UnitId` type lands, so `.slug()` has a home. Blast radius 1 (2-3 contained files, no published interface).

## precond-cli-subcommand-skeleton

- **Status**: proposed
- **Score**: 15/25 (leverage 2, locality 3, blast radius 1, heat 3)
- **Files**: ~2 estimated
- **Modules**: `core/_precond_cli.py:100-128` (`verify` handler), `:158-184` (`discharge` handler)
- **Summary**: The `verify` and `discharge` subcommand handlers repeat an `emit_only` early-out + `PreconditionUnavailable`→stderr→exit-code branch + `json`/headline-print skeleton, sharing even the `f"{args.source}::{args.function}: {result.label}"` headline. A shared prologue helper concentrates the shape.
- **First seen**: 2026-09-14
- **Reason**: leverage 2 — the differing tails (`verify` prints the counterexample; `discharge` prints per-caller lines) limit how much a helper concentrates; shallow but real. Low priority. The shared headline ties to `unit-id-value-type`.

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

## cli-json-or-render-epilogue

- **Status**: dropped
- **Score**: — (leverage 1)
- **Files**: n/a
- **Modules**: `src/forseti/core/cli.py` (`_run_propose`, `_run_submit_property`, `_run_check`, `_run_semantic_loop`, `_run_verify`)
- **Summary**: Every Core CLI handler ends with `if args.json: print(json.dumps(result.to_dict())) else: print(_render_X(result))`.
- **First seen**: 2026-09-04
- **Reason**: Leverage 1 — the render fn and the exit-code policy differ per command; a shared helper would take both as injected params, relocating the variation to the call site rather than concentrating it.

## codex-claude-verify-drift

- **Status**: dropped
- **Score**: — (fails deletion test)
- **Files**: n/a
- **Modules**: `src/forseti/adapters/codex/verify_hook.py`, `adapters/claude_code/post_tool_use.py`
- **Summary**: The Codex whole-file `forseti verify` subprocess + JSON `decision` path and the Claude in-process per-function `verify_and_record` + stderr/exit path are parallel "verify edits → block on counterexample → report" pipelines that drifted (lowercase verdict strings vs `UnitVerdict`; file vs function granularity; no shared state persistence on the Codex side).
- **First seen**: 2026-09-04
- **Reason**: Fails the deletion test — the mechanisms genuinely differ, so a shared seam would be a param-heavy switch; complexity moves/parameterises, it does not concentrate.
