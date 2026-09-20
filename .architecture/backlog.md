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

- **Status**: landed
- **PR**: #281
- **Score**: 19/25 (leverage 4, locality 4, blast radius 3, heat 4)
- **Files**: ~10 estimated
- **Modules**: construction at `core/propose.py:70`, `core/submit.py:77`, `orchestrator/ports.py:107`, `adapters/claude_code/property_gate.py:234`, `adapters/claude_code/forseti_gate.py:1662,1832,1912,2055,2254`, `adapters/oh_my_pi/verify_hook.py:239`; inverse parse `properties/proposer.py:87`; a new `make_unit_id`/`unit_id_symbol`/`unit_id_prefix` seam at top-level `src/forseti/unit_id.py` (stdlib-only leaf — top-level, not under `properties/`, so the two hooks that don't import `forseti.properties` today don't newly drag its eager `ClaudeCliClient`+sqlite init)
- **Summary**: The `path::symbol` unit-id convention has no owning module — ~10 sites hand-format it and one consumer (`proposer.py:87`) re-parses it with a silent malformed-id fallback, so producers and consumer can drift with no enforcement. Give it one constructor/parser with a pinned round-trip. Scope is the `::` **join + split** only; each caller keeps its current left-operand path spelling, so every produced string and DB key is byte-identical.
- **First seen**: 2026-09-11
- **Reason**: **picked 2026-09-14** — top-scoring eligible candidate (19/25) now that PR #275 landed and cleared the in-flight slot. Within 1 point of the runner-up candidate `proposal-request-prologue` (18/25), which overlaps only at the two Core producer lines (`propose.py:70`, `submit.py:77`). Scope settled at **join+split concentration, no normalization change**: the Core(raw `source`)-vs-gate(`..`-resolved rel via `forseti_gate.unit_id()`) left-operand asymmetry (`property_gate.py:44-55`) is a *deliberate, documented* residual — unifying it is a behaviour/wire change, out of scope for an unattended run and reported for a human. The consumer-side slug derivations (`orchestrator/persistence.py`, `orchestrator/check.py`) are filed separately as `unit-id-slug-derivations`, not folded in.

### Run 2026-09-14 — complete

- **Outcome**: complete
- **Stopped at**: step 6 — PR #281 opened; work landed on branch
- **Branch**: `pm-deepen/unit-id-value-type` — *created* as `pm-deepen/run-2026-09-14-0103` from `origin/main` and renamed at step 2. Branch adoption was **refused** at step 0 on condition 3: the firing branch (`sym/forseti/routine/refactor-audit/01M2EG403Q`) had an upstream (`@{u}` resolved to `origin/main`), so it was not a made-for-this-run, no-upstream branch.
- **Committed**: review report + design pass (winner: string-first free functions `forseti.unit_id`; runner-up design C — the rich `UnitId` value type — lost on the str-currency/blast-radius axes), the `refactor` implementation (new top-level leaf `src/forseti/unit_id.py` with `make_unit_id`/`unit_id_symbol`/`unit_id_prefix`; 8 producer/consumer files rewired; new `tests/properties/test_unit_id.py`), and this backlog update.
- **Evidence**: quality gate green — ruff check + ruff format --check + ty check + pytest (1672 passed, 1 skipped, ESBMC-gated included); project coverage 97.98% (gate 96%), `unit_id.py` 100%; PR #281. Diff 9 files vs ~10 estimate; no public/wire interface touched (every produced string byte-identical). The `advisor` was rate-limited at the pick and the design adjudication; the no-advisor fallback was used.
- **Next**: human review of PR #281 (do not merge as part of the routine). Natural next firing: the runner-up candidate `proposal-request-prologue` (18/25, within 1 point) — it overlaps only at the two Core `unit_id =` lines this PR swapped for `make_unit_id`, so the rest of its prologue is still extractable. `unit-id-slug-derivations` (18/25) becomes cleanly implementable once this leaf lands (a `slug()` then has a home).

### Run 2026-09-18 — reconciled

- **Outcome**: reconciled `in-flight` → `landed`. PR #281 merged 2026-09-15T19:13:53Z (`gh pr view 281 --json state,mergedAt`); on `origin/main` as `c17d557`.
- **Consequence for `unit-id-slug-derivations`**: partially unmet. #281 landed `src/forseti/unit_id.py` as **free functions** (`make_unit_id`, and the symbol/prefix accessors) — **not** a `UnitId` value type. That entry's premise, "once a `UnitId` type exists, a single `UnitId.slug()` concentrates both rules", therefore still has no class to hang `.slug()` on; a slug helper would have to land as another free function in the same leaf. Leverage held at 3, score held at 18/25.

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
- **Re-check 2026-09-18**: friction intact — the `read_text` → `extract_signature` → `ProposalRequest(...)` prologue still stands at `core/propose.py:74-78` and `core/submit.py:81-85` after #262 and #281. Stays `proposed` at 18/25.
- **Re-check 2026-09-21**: friction intact at `core/propose.py:70-80` and `core/submit.py:77-90`, but **one line shorter**: PR #281 replaced both hand-formatted `f"{source}::{function}"` spellings with `make_unit_id`, so the extractable prologue is 7 shared lines, not 8. New nuance to fold into whoever takes it — there are *three* definitions of "this unit's source text" in Core, and this extraction cements two of them while leaving the third: `propose.py:70` and `submit.py:77` use raw `source.read_text()`, while `check_source` goes through `Unit.from_path` (`orchestrator/ports.py:104-108`), which runs `rename_all_declarations_and_definitions(..., "main", _RENAMED_MAIN)`. So the proposer's LLM prompt and `extract_signature` see a `main` the checker will have renamed away. That is a **behaviour** asymmetry the extraction must not silently unify. Stays `proposed` at 18/25.

## counterexample-fired-label-predicate

- **Status**: proposed
- **Score**: 17/25 (leverage 3, locality 4, blast radius 2, heat 3)
- **Files**: ~3–4 estimated
- **Modules**: `src/forseti/esbmc/result.py` (`Violated`), `src/forseti/precond/verify.py`, `src/forseti/precond/discharge.py`
- **Summary**: Add a typed-first, raw-fallback label predicate on `Violated` and route the precond raw-trace substring scans through it, so the label-matching convention lives with the typed result model.
- **First seen**: 2026-09-02
- **Reason**: not to be folded into `precond-reachability-probe-tri-state` — switching precond off the raw scan is a behaviour change and must be reviewed on its own. Re-scan 2026-09-14: **three** direct raw scans, not four (`verify.py:145`, `discharge.py:640`, `reachability.py:55`; the earlier "4" counted `classify_site_probe`'s two call sites, which already route through `reachability.py:55`). Deletion-test caveat: `esbmc/counterexample.py`'s `ViolatedProperty` does not capture the ESBMC assert label, so a `Violated.fired(label)` predicate still substring-scans internally today — it concentrates the *convention* and gives one future upgrade point, rather than being a pure move. Score held at 17/25.
- **Re-check 2026-09-21**: **partially resolved by PR #294** — two of the three raw scans are gone. The probe scans are now behind one seam (`precond/reachability.py:44-59` `classify_site_probe`, which also refuses an empty label, plus `precond/run.py:108-121` `ProbeSite.label`, which pairs emitter and reader so they cannot drift), and `verify.py` no longer scans a counterexample at all. What survives is the **blame** scan: `precond/discharge.py:615` `if OBLIGATION_LABEL_PREFIX in result.raw_counterexample:` — the line deciding `OBLIGATION_VIOLATED` vs `CALLER_VIOLATED`. Rescoped to that one site; score drops to **15/25** (leverage 2 — a single remaining site; locality 4, blast radius 2, heat 3).

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
- **Re-check 2026-09-18**: now **three** harnesses, not two — `adapters/oh_my_pi/install.py` joined `claude_code/install.py` and `codex/install.py` when #270 landed. All three agree on the `tuple[Path, Enum]` return contract and each defines byte-identical `InstallOutcome`/`RemoveOutcome` enums. Leverage raised 3 → 4, so the score moves **14/25 → 17/25** (leverage 4, locality 3, blast radius 3, heat 3). Still below this run's pick; stays `proposed`.

## canonical-gate-decision-helper

- **Status**: proposed
- **Score**: 17/25 (leverage 3, locality 3, blast radius 2, heat 4)
- **Files**: ~4 estimated
- **Modules**: `src/forseti/core/events.py`, `adapters/claude_code/post_tool_use.py` (`_record_gate_decision`), `adapters/codex/verify_hook.py` (`_record_gate_decision`), `adapters/oh_my_pi/verify_hook.py` (`_record_gate_decision`), `adapters/claude_code/stop_gate.py` (`_record_semantic_gate_decision`)
- **Summary**: Move the canonical `gate.decision` event emitter into `core/events.py` beside its sibling `record_property_proposed`, as one `record_gate_decision(store_root, *, harness, adapter, decision, unit_ids=None, files=None, file=None)`, preserving the deliberate `unit_ids` (Claude/Oh-My-Pi) vs `files` (Codex) field asymmetry.
- **First seen**: 2026-09-02
- **Reason**: **now four sites, not three** (re-scan 2026-09-14): `post_tool_use.py:43-55`, `codex/verify_hook.py:184-207`, `oh_my_pi/verify_hook.py:294-311` (**missed by the prior scan**), and `stop_gate._record_semantic_gate_decision:231-249` (docstring at `:236` says "Mirrors `post_tool_use._record_gate_decision`"). Four emit sites across three harnesses in hot code; the sibling slot `record_property_proposed` already sits in `events.py:65-84`. Runner-up-tier at 17/25 (leverage held at 3 — a helper must still carry the store-root and payload-key divergences), the natural firing after `unit-id-value-type`. Could fold in as the emit sub-seam of the (now landed) `hook-verdict-report-two-hooks`.
- **Widened 2026-09-21**: the entry covers the four duplicated `_record_gate_decision` wrappers (`post_tool_use.py:44-56`, `stop_gate.py:231-250`, `codex/verify_hook.py:196-214`, `oh_my_pi/verify_hook.py:~300-315`) — but there is a **missing fifth emitter**. `adapters/claude_code/post_bash.py:44-78` emits the per-file `EDIT`/`VERIFY`/`GATE` trace and no canonical `gate.decision` at all, so a C file written out-of-band via Bash and blocked is invisible in the cross-harness trace; `tests/adapters/claude_code/test_out_of_band.py` never greps for `gate.decision`, which is why the gap survived. The same two hooks have also drifted on the decision itself: `post_bash.py:71` calls `verdict_report.partition` and hand-rolls `"block" if part.failures else "pass"` plus `exit_code=2 if part.failures else 0`, where `post_tool_use.py:139-141` uses the `Report.is_failure`/`.exit_code` PR #275 introduced. Filed here rather than as `post-bash-gate-trace-seam` so the fifth site is fixed with the other four. **Note**: adding the missing emission *adds* a record to a published wire format (`.forseti/events.jsonl`), so it wants calling out in the PR body, not folding in silently.

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
- **Re-check 2026-09-18**: friction intact — `orchestrator/persistence.py:30` (`_unit_slug`) and `orchestrator/check.py:417` (`_harness_filename`) both still exist and still sanitize differently. Premise only partly satisfied by #281 (free functions, no `UnitId` type — see that entry's 2026-09-18 reconciliation). Stays `proposed` at 18/25.
- **Re-check 2026-09-21**: friction intact; `orchestrator/persistence.py:174-185` (`_unit_slug`) and `orchestrator/check.py:417-426` (`_harness_filename`) both still sanitize differently. Stays `proposed` at 18/25.

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

## precond-sidecar-run-seam

- **Status**: landed
- **PR**: #294
- **Score**: 21/25 (leverage 4, locality 5, blast radius 2, heat 4)
- **Files**: ~4–6 estimated
- **Modules**: `src/forseti/precond/verify.py:315-332` (`_run`), `:367-383` (`_assess_non_vacuity`), `src/forseti/precond/discharge.py:628-637` and `:684-687` (`_check_caller`), a new leaf `src/forseti/precond/run.py`
- **Summary**: "Run a sidecar" is one recipe — derive a filename under `work_dir`, `write_text(render_sidecar(...))`, climb `precondition_ladder` through `escalating_port`, then re-render the probe variant and run it **unescalated** at the settled `k` — and it is written out twice in full. The escalate-vs-don't-escalate asymmetry between the laddered run and the probe is load-bearing and stated nowhere; it survives only because both copies happen to agree. Give it one tested home so both drivers shrink to "render this, read the verdict".
- **First seen**: 2026-09-18
- **Reason**: **picked 2026-09-18** — top score. Separated from a four-way 20/25 tie (`forseti-cli-json-subprocess-seam`, `cli-check-phase-argument-block`, `verify-port-test-doubles`, `scanned-source-carrier`) by locality alone: the protocol spans two source files today, so it earns the rubric's "several files → one file" 5, where `cli-check-phase-argument-block`'s two blocks sit inside one file and cap at 4. Also the only one of the five that is fully esbmc-free (both drivers take an injected `VerifyPort`; `tests/precond` runs 126 tests in 0.54s) and touches no published interface.
- **Re-check 2026-09-21**: **landed.** `gh pr view 294` reports `MERGED` at 2026-09-18T07:49:38Z (`c5b431c` on `main`); `src/forseti/precond/run.py` and `tests/precond/test_precond_run.py` exist and are covered by 14 hermetic tests. Two residues, recorded on their own entries rather than reopening this one: the extraction took the *primitives* (`SidecarRunner.climb`/`.probe`) and not the *choreography* — `verify._run` + `_assess_non_vacuity` and `discharge._check_caller` still walk the identical climb → dispatch → probe → three-arm `match` shape by hand; and `precondition_ladder` and `sidecar_verify_port` still have zero direct test references.

## forseti-cli-json-subprocess-seam

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 5, blast radius 3, heat 4)
- **Files**: ~10 estimated
- **Modules**: `adapters/claude_code/forseti_gate.py:803-846` (`_list_units`) and `:1675-1734` (`verify_function`), `adapters/claude_code/property_gate.py:295-349` (`_check_unit`), `adapters/codex/verify_hook.py:81-103` (`_verify`), `adapters/oh_my_pi/verify_hook.py:114-134` (`_list_functions`) and `:137-186` (`_semantic_check`), a new `adapters/_forseti_cli.py`
- **Summary**: Six sites across three harnesses hand-roll the same four decisions — argv assembly, timeout, failure bucketing, JSON decode — and have already drifted four ways: codex and oh-my-pi forward **no** `FORSETI_BUILD_FLAGS` where `property_gate.py:311-317` documents why they must; the `verdict`/`counterexample or reason or message` decoder exists twice; the stderr-fallback clip is `[:800]`/`[:400]`/`[:400]`; and the launch/timeout/decode failure taxonomy is three-way on the Claude side and one-way on the other two. A leaf returning a structured run outcome lets each caller keep its own translation tail.
- **First seen**: 2026-09-18
- **Reason**: runner-up candidate this run, within 1 point; the natural next firing. **Not** a re-run of the dropped `codex-claude-verify-drift` — that entry judged the *pipelines* irreconcilable (per-file vs per-function), which still holds; this is only the subprocess boundary underneath them, identical in all six sites. Carries one **behaviour** finding an unattended run must not bundle in: forwarding build flags on the codex/oh-my-pi paths changes their verdicts on any project with `-I`/`-D`. File that as a bug for a human; keep the extraction behaviour-preserving. Note also the argv half is currently **unpinned** at two of three harnesses (both suites stub `fake_run(*_a, **_kw)` and ignore their arguments), so argv-shape assertions must land before the change.
- **Re-check 2026-09-21**: friction intact; all six sites re-confirmed at `forseti_gate.py:803` (`_list_units`), `:1646` (`verify_function`), `property_gate.py:257` (`_check_unit`), `codex/verify_hook.py:82` (`_verify`), `oh_my_pi/verify_hook.py:114` (`_list_functions`) and `:137` (`_semantic_check`). Stays `proposed` at 20/25.

## cli-check-phase-argument-block

- **Status**: proposed
- **Score**: 20/25 (leverage 3, locality 4, blast radius 1, heat 5)
- **Files**: ~1 estimated
- **Modules**: `src/forseti/core/cli.py:524-561` (the `check` subparser) and `:730-767` (the `semantic-loop` subparser); existing precedent at `:274-289` (`_add_unit_store_arguments`) and `core/_precond_cli.py:36-73` (`_add_precondition_arguments`)
- **Summary**: The two check-phase flag blocks are character-for-character identical apart from two prose strings, including the 6-line f-string help that interpolates `CHECK_DEFAULT_UNWIND_LADDER` — so changing the documented ladder default has to be done twice or the two subcommands silently document different defaults. The extraction was already started twice in this codebase and stopped one block short.
- **First seen**: 2026-09-18
- **Reason**: tied at 20/25, in the hottest source file in the tree (16 of the last 60 commits), and the lowest-risk of the band (one file, argparse surface unchanged, both parsers already exercised through `cli.main`). Lost the pick on locality only: both sites are inside `cli.py`, so the rubric's "several **files** → one file" clause that earns a 5 is definitionally unavailable to it.
- **Re-check 2026-09-21**: friction intact at HEAD `f888cf0`; the twin blocks are now `cli.py:526-563` (`check`) and `:732-769` (`semantic-loop`), with the `--unwind-ladder` f-string byte-identical at `:538-542` and `:744-748` and only the two predicted prose deltas (`--json` help at `:555` vs `:761`; passthrough example at `:561` vs `:767`). **Runner-up candidate 2026-09-21** — won the four-way 20/25 tie-break on the rubric's first criterion, lower blast radius (1). Lost the pick to `cli-command-trace-wrapper` by 2 points, on locality and leverage. Stays `proposed` at 20/25.

## verify-port-test-doubles

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 5, blast radius 3, heat 4)
- **Files**: ~15 estimated
- **Modules**: `class FakeVerify` verbatim in 11 modules (`tests/orchestrator/test_loop.py`, `test_ladder.py`, `test_check.py`, `test_telemetry.py`, `test_report.py`, `test_transcript.py`, `test_persistence.py`, `test_fix.py`; `tests/core/test_core_check.py`, `test_core_loop.py`, `test_semantic_loop_cli.py`), 35 hand-rolled `RunMeta`/`_meta` sites; `tests/esbmc/conftest.py` is 0 bytes and `tests/orchestrator/`, `tests/core/` have no conftest
- **Summary**: Every driver test pays ~30 lines of scaffolding before its first assertion, and the copies have drifted — some record `calls`, some `unwinds`, `test_telemetry.py:61` records neither. "What a scripted ESBMC verdict looks like" is one concept spread over ~18 files; a shared `ScriptedVerify` plus verdict/`RunMeta` builders is where it wants to live.
- **First seen**: 2026-09-18
- **Reason**: tied at 20/25. Test-only, so leverage caps at 4 — no production interface gets deeper. One constraint for whoever takes it: `tests/orchestrator/test_report.py:127` (`test_k_reflects_the_escalated_bound_not_the_argv`) deliberately relies on the meta's `argv` being *stale* relative to `k`, so a shared builder must let the caller fix `argv` rather than deriving it from `unwind`.
- **Re-check 2026-09-21**: friction intact; fresh `FakeVerify` class definitions re-counted at `tests/core/test_core_check.py:55`, `tests/core/test_core_loop.py:63`, `tests/orchestrator/test_persistence.py:26`, plus the `test_check.py`/`test_telemetry.py`/`test_loop.py` copies. Stays `proposed` at 20/25.

## scanned-source-carrier

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 4, blast radius 3, heat 5)
- **Files**: ~3 estimated (but see the scope warning)
- **Modules**: `src/forseti/esbmc/units.py:1466-1506` (`_with_predefined_guards`), `:1210-1236` (`_annotate_array_extents`), `:1199-1209` (`_candidates_by_name`), `:1508-1550` (`list_units`), `:900-975` (`_select_definition`), `:32-35` (four private imports from `preprocessor`); `_stripped_for_scan` re-derived at `:1001`, `:1082`, `:1194`, `:1533`
- **Summary**: There is no carrier for "this source, scanned". The masked text, per-name definition candidates, `#line` breakpoints and the measured `predefined` guard seed are four artifacts derived from one string, and every function in the chain either re-derives them or takes them as extra positional parameters — a 4-deep hand-threading plus a second, uncoordinated derivation path for the public entry points.
- **First seen**: 2026-09-18
- **Reason**: tied at 20/25 but **deliberately not picked**: highest regression risk of the band. `units.py`'s scan heuristics have historically required differential runs against a `clang __LINE__` oracle to prove equivalence — more verification than one unattended firing can carry. Scope warning: `find_definition_brace` is called from `precond/synth.py:575` and `properties/harness.py:517`, so the change must either freeze that public signature or accept a wider blast radius.
- **Re-check 2026-09-21**: friction intact — `esbmc/units.py:1530-1548` still threads `masked` and `candidates` as loose positional args into `_with_predefined_guards` and `_annotate_array_extents`. Still not picked, for the same regression-risk reason. Stays `proposed` at 20/25.

## harness-reply-triage-two-hooks

- **Status**: proposed
- **Score**: 19/25 (leverage 4, locality 4, blast radius 2, heat 3)
- **Files**: ~4 estimated
- **Modules**: `adapters/codex/verify_hook.py:120-181`, `adapters/oh_my_pi/verify_hook.py:217-292`; in-repo template at `adapters/claude_code/verdict_report.py:25-119` (`ReportStyle`/`partition`/`render`/`emit`)
- **Summary**: Both gates own an identical triage-and-reply grammar — partition into violated/inconclusive, `### VIOLATED: {id}` blocks, an "Also inconclusive (do not ignore)" tail, the same "Not a pass — raise k, add an entry/harness, or report." sentence — and they have already drifted ("could not conclusively **verify**" vs "**check**"). The residual builder `", ".join(f"{x} [{y}]")` appears four times.
- **First seen**: 2026-09-18
- **Reason**: distinct from the landed `hook-verdict-report-two-hooks`, which was `UnitVerdict[]` → stderr/exit-code *inside* Claude Code; this is `(id, evidence)[]` → stdout JSON across two *other* harnesses, and `verdict_report.ReportStyle` is the in-repo template for the target shape. Well pinned already.

## open-caller-checks-openings-record

- **Status**: proposed
- **Score**: 18/25 (leverage 3, locality 4, blast radius 1, heat 3)
- **Files**: ~3 estimated
- **Modules**: `precond/discharge.py:206-215` (signature), `:234-285` (five near-identical comprehensions), `:305-316` (`find_open_callers`); the record `CallerOpenings` already exists at `esbmc/units.py:1411-1436`; test helpers at `tests/precond/test_open_callers.py:23-33`, `tests/precond/test_discharge.py:124-145`
- **Summary**: `open_caller_checks` takes the five openings as five separate keyword-only `tuple[str, ...]` params, and `find_open_callers` destructures a `CallerOpenings` it already holds back into those five keywords — interface tax as complex as the implementation it fronts. Adding a sixth way the caller set can be open is a four-site lockstep edit rather than one table row.
- **First seen**: 2026-09-18
- **Reason**: the **consumer-side** shape; deliberately not folded into `esbmc-caller-openings-module-split`, which relocates the ~460 LOC of parsing inside `units.py`. Pinned by `tests/precond/test_open_callers.py:76` (`test_the_concatenation_order_is_stable`), exactly the invariant a table-driven rewrite must preserve.
- **Re-check 2026-09-21**: friction intact and **stronger than first filed**. `find_open_callers` (`discharge.py:283-311`) has **zero** test references anywhere under `tests/` — every discharge test injects `open_callers_fn` — so the five hand-written keyword pairings at `:303-311` are never executed. Swapping `escaped=`/`aliased=` would pass the entire suite and silently mislabel why a discharge was withheld. That untested adapter between two records is precisely what the fix deletes. Stays `proposed` at 18/25.

## harness-registry-dispatch-table

- **Status**: proposed
- **Score**: 17/25 (leverage 3, locality 3, blast radius 2, heat 4)
- **Files**: ~3 estimated
- **Modules**: `adapters/harness.py:30-35` (the enum that names harnesses but carries no facts), `core/cli.py:1035-1094` (`_run_enable_project`), `:1130-1173` (`_run_disable_project`), `:894-915`, `:937-941`, `:961-965` (three near-identical hook dispatchers)
- **Summary**: Install fn, remove fn, error type, whether `--shared` applies, and the CLI prose are spread across six branches in two handlers, with the `--shared has no effect` guard copied four times. All three harnesses already agree on the `tuple[Path, Enum]` return contract, so the registry is one dataclass per harness away.
- **First seen**: 2026-09-18
- **Reason**: weak concentration — the outcome-enum half of the win overlaps `adapter-install-skeleton-two-harnesses`. Adjacent to the dropped `cli-run-handler-shape`, but distinct: that entry was about the generic `try/except/print/return 1` shape (already extracted as `_run_harness_action` at `core/cli.py:1015-1032`), not the per-harness facts table.
- **Widened 2026-09-21**: the same "one dataclass per harness" shape also governs a *second* handler pair the entry did not name — the hook subcommands. `core/cli.py:905-921`/`:924-946` (claude-code), `:949-964`/`:967-970` (codex), `:973-988`/`:991-994` (oh-my-pi) are three `_add_*_hook_parser` functions differing only in name, prose and which `HOOK_NAMES` feeds `choices`, plus three `_run_*_hook` dispatchers of which two are a single-branch `if` and a copied `raise AssertionError`; a fourth harness also needs a fourth entry in the unrelated hardcoded set at `cli.py:1266-1271`. Filed here rather than as `hook-subcommand-dispatch-triple` so the two are taken together. **Constraint**: `cli.py:925-929` documents the Claude branch's four per-invocation lazy `main()` imports as deliberate — each hook is its own short-lived process — so any table must keep the import *inside* the dispatch callable, not at module scope. **Constraint**: `tests/core/test_core_cli_dispatch.py:33-65` hard-codes the 14-name subcommand set and asserts handler identity `cli._run_<name>`, so those three handler names must survive. Score held at 17/25 — the widening adds sites, not leverage per site.

## pointer-length-pairing-two-parsers

- **Status**: proposed
- **Score**: 17/25 (leverage 4, locality 4, blast radius 4, heat 3)
- **Files**: ~5 estimated
- **Modules**: `properties/signature.py:189-223`, `:96-100`, `:232-236`; `precond/synth.py:87-88`, `:175-176`, `:185-194`, `:258-295`
- **Summary**: Both modules answer "does this pointer parameter pair with the next integer, and in what units?" and have drifted into different answers. `signature.py` pairs any following non-pointer integer and always treats the length as an **element count**; `synth.py` pairs only on a name allowlist and distinguishes **byte length** from **element count**. So `f(int *p, size_t nbytes)` gets an `nbytes`-element object on the properties path and an `nbytes`-*byte* object on the precond path. The integer predicates also disagree: `synth.py:176` is a bare substring scan, so a `point_t count` parameter reads as an integer length because `"int" in "point_t"`.
- **First seen**: 2026-09-18
- **Reason**: blast radius 4 — reconciling byte-length against element-count decides **what a generated harness allocates**, which is a behaviour decision, not a refactor. An unattended run must not settle it silently; it wants a human and a before/after differential. Only the pure ordered-parameter → (buffer, length, units) rule concentrates — merging the two *parsers* (regex over a source slice vs a clang `Param` list) gains nothing.

## update-notice-emission-policy

- **Status**: proposed
- **Score**: 16/25 (leverage 3, locality 4, blast radius 2, heat 2)
- **Files**: ~4 estimated
- **Modules**: `src/forseti/update_notice.py:35-65`, `core/cli.py:1236-1244`, `esbmc/cli.py:37-40`
- **Summary**: `update_notice()` returns a banner and leaves "may I print this, and where" to every caller, so the policy lives nowhere: one inline `if` in `core/cli.py` suppressing four subcommands, and no suppression at all in `esbmc/cli.py`. `verify`, `list-units` and `semantic-loop` are *not* suppressed and are exactly the commands the three hook adapters shell out to — while all three read stderr as evidence on the failure path (`codex/verify_hook.py:95`, `oh_my_pi/verify_hook.py:164`, `forseti_gate.py:1730`), so the banner can become the reported skip reason handed back to the model.
- **First seen**: 2026-09-18
- **Reason**: two emit sites = a real seam, not a hypothetical one, but cold code (heat 2). The *suppression* behaviour has no existing assertion — a test that `forseti codex-hook` emits no banner while `forseti verify` does must land first.

## esbmc-run-boundary

- **Status**: proposed
- **Score**: 15/25 (leverage 2, locality 3, blast radius 2, heat 4)
- **Files**: ~3 estimated
- **Modules**: `esbmc/units.py:1237-1262` (`_parse_tree`), `:325-331` (`_error_line`), `:1296-1412` (`probe_predefined_guards`); `esbmc/runner.py:209-285` (`verify`), `:58-63` (`_error_message`)
- **Summary**: The `subprocess.run` invocation shape, "esbmc's output is stdout ⧺ stderr", and "an error is the first `ERROR:` line" are each spelled twice, and the `ERROR:` extractor has drifted on its fallback (`""` vs `"esbmc reported an error"`). Timeout policy has three answers in three places (`+ VERIFY_GRACE_S`, raw, `min(…, cap)`).
- **First seen**: 2026-09-18
- **Reason**: leverage 2 — once the divergent translation policy (fail-loud raise vs typed verdict) is subtracted, the shared core is roughly 12 lines. A drift/tidiness candidate, not a leverage one.

## nondet-generator-convention-two-slugs

- **Status**: proposed
- **Score**: 14/25 (leverage 2, locality 3, blast radius 2, heat 3)
- **Files**: ~4 estimated
- **Modules**: `properties/harness.py:331-338`, `:341-358`, `:175-182`; `precond/synth.py:319-322`, `:413-421`
- **Summary**: Two same-named private `_nondet_slug` functions with the same stated purpose — name the `nondet_*` generator ESBMC will model for a C type — implemented with different regexes, and they disagree: `_Bool` → `nondet__Bool` vs `nondet_Bool`; `const char *` → `nondet_const_char__` vs `nondet_const_char`; `_Atomic(int)` → `nondet__Atomic_int_` vs `nondet_Atomic_int`. Neither is broken today (each emitter uses its own slug for both prototype and call site), but the one external fact is stated twice, plus a gratuitous `extern` divergence.
- **First seen**: 2026-09-18
- **Reason**: weakest concentration — only the naming/declaration convention. The two emitters build genuinely different artefacts (`render_semantic_harness` inlines the unit source, `render_sidecar` `#include`s it), so there is no deep "one harness writer" hiding here. The fix must *pick* a spelling, which changes emitted C on one side.

## cli-command-trace-wrapper

- **Status**: in-flight
- **PR**: #307
- **Score**: 22/25 (leverage 4, locality 5, blast radius 2, heat 5)
- **Files**: ~4–5 estimated
- **Modules**: `src/forseti/core/_precond_cli.py:113-138` (`_traced`), used at `:141-142` and `:205-206`; `src/forseti/core/cli.py:801-823` (`_run_semantic_loop`); `src/forseti/core/events.py:38-42` (the policy comment beside `CLI_COMMAND`); `docs/design/0001-harness-portability.md:131` (the one published contract row covering both emitters)
- **Summary**: The "one `cli.command` event per invocation, on every exit path" policy is implemented twice — `_traced` in the precond glue module and an inline clock/`record_event`/return block in `_run_semantic_loop` — with the *reasoning* duplicated in parallel prose too (`_precond_cli.py:120-123` against `cli.py:804-808`). Both copies landed together in `7bcc812` (#301/#303). The shared half is parked in the wrong module: `_precond_cli.py:1-10` documents itself as precond-only glue, so a general CLI-tracing concern lives inside the precondition subcommand file and `core/cli.py` cannot reach it without importing precond glue. One seam owning the clock, the common five fields (`command`, `source`, `function`, `exit_code`, `duration_s`) and the single-emission guarantee leaves each call site stating only its command-specific extras.
- **First seen**: 2026-09-21
- **Reason**: **picked 2026-09-21** — top score, 2 points clear of the runner-up candidate `cli-check-phase-argument-block` (20/25), so not a close pick. Newest duplication in the tree (three days old); behaviour-preserving against an already-published oracle (`events.py:38-41` and the design-doc row); strongly pinned by esbmc-free tests on both sides (`tests/core/test_precond_cli.py:266-290` parametrized over two commands × three exit paths, `tests/core/test_semantic_loop_cli.py:568-665`). **Constraint**: `tests/core/test_core_cli_dispatch.py:62-65` and `tests/core/test_precond_cli.py:57` pin handler *identity*, so `_run_synth`/`_run_discharge`/`_run_semantic_loop` must keep their names and module bindings.

## precond-unit-lister-default-four-copies

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 5, blast radius 3, heat 4)
- **Files**: ~5 estimated
- **Modules**: `src/forseti/precond/verify.py:243-245` (`verify_precondition`, forwards `timeout_s`), `:277` (`synthesize`, drops it), `src/forseti/precond/discharge.py:344` (`emit_obligations`, drops it), `:385-388` (`discharge_precondition`, forwards it), `verify.py:173-200` (`plan_for`); `core/_precond_cli.py:60-67`, `:148-153`, `:212-216`
- **Summary**: Four entry points restate the same `list_units_fn or (lambda src: list_units(src, esbmc_bin=..., ...))` prologue before calling `plan_for`, and the copies have drifted: two forward the caller's `timeout_s`, two fall through to `list_units`' own `timeout_s: float = 30.0` (`esbmc/units.py:1512`). `tests/precond/test_discharge.py:679-713` states the invariant explicitly — every ESBMC parse-tree run a driver makes on its own must honour the requested timeout — and the siblings violate it with no test to notice, so `forseti synth --emit-only -t 5` runs its probe with a 30 s budget. One `unit_lister(esbmc_bin, timeout_s)` seam makes the drift unrepresentable.
- **First seen**: 2026-09-21
- **Reason**: lost the pick to `cli-command-trace-wrapper` by 2 points. Carries a **CLI behaviour change** an unattended run must call out rather than fold in: honouring `-t` on `--emit-only` is corrective, not behaviour-preserving. Test-first precondition: `emit_obligations` — a published export backing `forseti discharge --emit-only` — has **zero** tests under `tests/precond/`; its default lister, its `SynthError → NEEDS_CONTRACT` mapping (`discharge.py:350-353`) and its `OSError → ERROR` mapping (`:354-357`) are entirely unexercised, so pins must land first.

## sibling-snapshot-staging-seam

- **Status**: proposed
- **Score**: 20/25 (leverage 4, locality 5, blast radius 2, heat 3)
- **Files**: ~2–3 estimated
- **Modules**: `src/forseti/adapters/claude_code/forseti_gate.py:552-765` (`_enumerable_source`), `:1484-1643` (`_verifiable_source`), `:414-444` (`_kernel_dir`), `:446-552` (`_index_ignore_snapshot`), `:957-996` (`_untracked_snapshot`), `:286-309` (the two prefixes)
- **Summary**: Two context managers stage an immutable sibling snapshot beside a source, yield the path and clean up, and have measurably drifted three ways. Directory resolution: `:670` uses `_kernel_dir(os.path.dirname(spelled))`, `:1608` does the lexical thing the enumerate side documents at `:604-616` as staging beside the wrong translation unit. Index protection: the enumerate side registers `.git/info/exclude` and verifies with a real-path `git check-ignore`, failing closed; the verify side does neither, so a concurrent `git add -A` can commit a verify snapshot. Cleanup: `:755-765` raises `UnitsUnavailable` on a failed unlink, `:1641-1643` silently suppresses it, leaving a full source copy in the tree.
- **First seen**: 2026-09-21
- **Reason**: scored 20/25 but **Worth exploring, not Strong**. Reconciling the two directory resolutions is a behaviour change in the gate's most load-bearing path, and the same-directory-vs-mirrored question already has a recorded residual that wants a human. The three drifts should be reported as findings alongside the extraction, never silently unified. Coverage is asymmetric by design of the accident: ~15 staging tests on the enumerate side (`tests/adapters/claude_code/test_gate_pointer.py:441`, `:567`, `:599`, `:693`, `:853`, `:898`, `:929`, `:960`, `:1057`, `:1087`, `:1114`, `:1142`), and nothing about exclude, kernel paths or cleanup on the verify side.

## renderability-authority-misses-identifiers

- **Status**: proposed
- **Score**: 19/25 (leverage 4, locality 4, blast radius 3, heat 4)
- **Files**: ~4 estimated
- **Modules**: `src/forseti/properties/harness.py:261-338` (`renderability_reason`, docstring claim at `:262-264`), `:150-160` (`render_semantic_harness`), `:325-334`; `src/forseti/properties/proposer.py:415-438` (`validate_candidate`), `:427-431`
- **Summary**: `renderability_reason` documents itself as "the single static authority on whether a `(signature, spec)` pair can become a valid harness" and `render_semantic_harness` delegates every `(signature, spec)` guard to it — but the identifier-existence rule lives only in the proposer's gate, so a caller holding the two documented inputs gets an incomplete check. Verified by running it: `render_semantic_harness` with spec `result >= y` over a signature whose only param is `x` emits `__ESBMC_assert((result >= y), "forseti:semantic");` — an undeclared `y`, no `HarnessError`. Second symptom: `proposer.py:432-435` re-expresses the output-parameter rule in a second vocabulary and is shadowed for output params because `renderability_reason` is consulted first.
- **First seen**: 2026-09-21
- **Reason**: partly a **bug fix on a published export** — `render_semantic_harness` would start raising `HarnessError` where it emitted C — which an unattended run should not bundle into a refactor. Exposure is narrowed by the CLI paths: `orchestrator/check.py:255 → render_property_harness → extract_signature` re-parses and fails loud on a parse miss, so the demonstrated hole is on the direct API, not on `forseti propose`. Pinned by 17 `test_renderability_reason_*` tests (`tests/properties/test_harness.py:251-437`) and five `validate_*` tests (`tests/properties/test_proposer.py:267-325`); none need esbmc.

## stop-gate-turn-outcome-carrier

- **Status**: proposed
- **Score**: 19/25 (leverage 4, locality 4, blast radius 2, heat 3)
- **Files**: ~2–3 estimated
- **Modules**: `src/forseti/adapters/claude_code/stop_gate.py:252-462` (`main`), `:373-383`, `:409-423`, `:435-449` (the same five-field event tail three times), `:44`, `:58`, `:68`, `:88`, `:127`, `:131`, `:145`
- **Summary**: `main()` computes five independent outstanding kinds (`blocking`, `oob`, `blob`, `needs`, `semantic`) and then re-derives from them at four separate exits — `outstanding` (`:298`), `n_out` (`:407`), the `decision` label (`:369`), the event tail (3×) and the section list (`:398-404`). There is no value that says "this turn's tally", which is why `:373` and `:409` differ in which fields they carry. The seven `_*_message` helpers are each a header plus a join, and exist only because `main()` has no carrier to hang rendering off.
- **First seen**: 2026-09-21
- **Reason**: constraint for whoever takes it — the `stop` event field names in `.forseti/events.jsonl` are a trace format consumers filter on (`stop_gate.py:298-303` explicitly warns about a false positive from mislabelling), so the emitted keys must stay byte-identical. Pinned by `tests/adapters/claude_code/test_stop_gate_semantic.py:75`, `:105`, `:132`, `:295`, `:365`, with the `decision`/`n_*` fields asserted directly at `:317`, `:333`; no esbmc.

## gate-state-carrier

- **Status**: proposed
- **Score**: 18/25 (leverage 4, locality 4, blast radius 3, heat 3)
- **Files**: ~8 estimated
- **Modules**: `src/forseti/adapters/claude_code/forseti_gate.py:1786-1795` (`_fresh_state`), `:1797-1808` (`load_state`), `:1810-1816` (`save_state`), `:1818-1820` (`record`), plus ~24 raw-key sites; `stop_gate.py:282,310,312,350,351`; `post_bash.py:103`; `property_gate.py:207`
- **Summary**: The gate's persisted state is a bare `dict[str, Any]` with seven string keys (`units`, `stop_attempts`, `scanned`, `pending`, `baseline_blobs`, `baseline_head`, `properties`), and 23 signatures under `src/forseti/adapters/` spell `state: dict[str, Any]`. Understanding "what is a pending marker" means bouncing between `_pending_attempts:1185`, `_pending_owner:1199`, the literal `{"hash", "attempts", "pid"}` built at `:2248`, and the defensive re-reads at `:1185-1214`; every consumer re-does the `state.get("scanned", {})` defaulting `load_state:1802-1804` already did.
- **First seen**: 2026-09-21
- **Reason**: constraint — `.forseti/gate_state.json` persists across sessions (`save_state:1815` serializes with `sort_keys=True`), so the JSON key names must not change even when the in-memory carrier does.

## param-union-dispatch-three-walks

- **Status**: proposed
- **Score**: 18/25 (leverage 3, locality 4, blast radius 1, heat 3)
- **Files**: ~2 estimated
- **Modules**: `src/forseti/properties/signature.py:55` (`Param = ScalarParam | BufferParam`), `src/forseti/properties/harness.py:167-173`, `:351-368` (`_nondet_ctypes`), `:414-421` (`_call_arg`)
- **Summary**: `render_semantic_harness` walks `signature.params` three times with three hand-written isinstance dispatches over a two-member sealed union, and the three disagree on what an unknown member means: raise, **silently skip**, raise. The duplicated raise at `:421` is dead — `_call_arg` is first reached at `:201`, after the `:167-173` loop has already raised — so it is uncovered defensive code that looks like it is pulling weight. Adding a third `Param` member (the module anticipates one) makes `_nondet_ctypes` emit no generator for it, so the harness compiles with an uninitialised parameter and ESBMC checks the wrong program: the one walk that does not fail loud is the one deciding what gets nondet-filled.
- **First seen**: 2026-09-21
- **Reason**: lowest blast radius of the fresh band (2 private files), but leverage 3 — it moves less mass than the entries above it. Pinned by `tests/properties/test_harness.py:675` (`test_unknown_param_subtype_is_error`, which hits `:173` and never `:421`), `:54`, `:34`, `:80`, `:169`, `:239`; no esbmc.

## repo-scope-resolution-prologue

- **Status**: proposed
- **Score**: 18/25 (leverage 3, locality 4, blast radius 1, heat 3)
- **Files**: ~2 estimated
- **Modules**: `src/forseti/adapters/claude_code/forseti_gate.py:1148-1169` (`discover_changed_c_sources`), `:1347-1367` (`divergent_blob_sources`), `:1403-1411` (`baseline_blob_hashes`), `:998-1126` (`_in_scope_c_abspath`)
- **Summary**: Three sibling scans repeat the same four-line prologue — `rev-parse --show-toplevel`, the `None` bail, `.strip()`, `os.path.realpath(project_dir)` — purely to satisfy `_in_scope_c_abspath`'s four-argument signature, then loop calling it. The three pieces of context (`project_dir`, `root`, `proj_real`) are one concept, "this project's position in its repository", with no name, so the aliasing rationale is re-explained at the call site (`:1159-1165`) instead of living once behind the seam.
- **First seen**: 2026-09-21
- **Reason**: cheapest of the `forseti_gate.py` candidates and it sits in the file that most needs fewer loose parameters, but lower leverage than `sibling-snapshot-staging-seam`. `_in_scope_c_abspath` has no direct unit test; it is pinned only indirectly through the three public scans in `tests/adapters/claude_code/test_out_of_band.py`.

## semantic-loop-mode-vocabulary

- **Status**: proposed
- **Score**: 18/25 (leverage 3, locality 4, blast radius 2, heat 4)
- **Files**: ~4–5 estimated
- **Modules**: `src/forseti/core/loop.py:78` (`LoopMode`), `:167`, `:181`, `:209-212` (the `match` arms); `src/forseti/core/cli.py:658-663` (argparse `choices`, hyphen), `:855-857` (the `cast` + `replace("-", "_")`); `src/forseti/core/mcp_server.py:283-285` (a hand-rolled membership check, underscore)
- **Summary**: The three legal modes are restated in four places and the module that owns `LoopMode` owns none of the parsing. Two published faces publish different spellings of the same value — the CLI contract says `--mode check-only`, the MCP tool's `mode` argument says `check_only` — bridged only by a `cast` in a CLI helper. Only `run_semantic_loop`'s `match` is exhaustiveness-checked; the argparse tuple and the MCP tuple are plain literals a fourth mode can silently miss.
- **First seen**: 2026-09-21
- **Reason**: scoped deliberately to the **vocabulary only**. The three-altitude precondition checks (`loop.py:157-163` vs `cli.py:831-853` vs `mcp_server.py:283-284`) are *not* folded in: `cli.py:843-847` carries an explicit comment saying that duplicate check is deliberate and why, and `cli-run-handler-shape` was already dropped on adjacent reasoning. The change must be **additive** (accept both spellings, keep both faces emitting what they emit today), never a rename. `tests/core/test_core_mcp_server.py:243-247` pins the literal message `"mode must be one of"`.

## assessment-vocabulary-four-tables

- **Status**: proposed
- **Score**: 17/25 (leverage 3, locality 4, blast radius 3, heat 4)
- **Files**: ~5 estimated
- **Modules**: `src/forseti/precond/verify.py:65-74` (`Assessment`), `:89-111` (`PreconditionResult.label`), `:113-129` (`to_dict`), `:206-214` (`ASSESSMENT_EXIT_CODES`); `src/forseti/precond/model.py:81-99` (`DischargeResult.label`), `:101-107` (`to_dict`, which mutates the dict `verify.py` built)
- **Summary**: One 7-member vocabulary is interpreted by four hand-maintained tables across two modules, none exhaustiveness-checked, plus a `--json` payload assembled by one module and mutated by another. Verified: `PreconditionResult('f', Assessment.DISCHARGED_VERIFIED, ...).label` returns `'ERROR (...)'` — the fall-through at `verify.py:111` silently mislabels a pass as a tooling failure. Not reachable through today's drivers, but it proves the if-chains have no exhaustiveness net: a new member gets `"ERROR (...)"` from `.label` and a `KeyError` from `ASSESSMENT_EXIT_CODES` at `_precond_cli.py:176`.
- **First seen**: 2026-09-21
- **Reason**: constraint — `ASSESSMENT_EXIT_CODES` is the documented CLI exit-code contract (referenced from `core/cli.py:18`) and both `to_dict`s are the `--json` wire format (`_precond_cli.py:168`, `:225`); the key set and the integer mapping must stay byte-identical. `tests/precond/test_discharge.py:675-676` guards only the dict (`set(ASSESSMENT_EXIT_CODES) == set(Assessment)`), not the labels.

## check-source-event-emission-bypass

- **Status**: proposed
- **Score**: 17/25 (leverage 3, locality 4, blast radius 3, heat 4)
- **Files**: ~4–6 estimated
- **Modules**: `src/forseti/core/check.py:177` (one per-*run* `PROPERTY_CHECK_START`), `:178-194` (the `check_properties` call, with no `sink=`), `:195-203` (the post-hoc verdict loop); `src/forseti/orchestrator/check.py:288`, `:296-302`, `:331-336`, `:373-379`; `src/forseti/orchestrator/telemetry.py:152-182` (`EventSink`/`EventEmitter`, the unused seam)
- **Summary**: The same two event-type strings are produced by two independent emission systems with incompatible shapes and cardinality — orchestrator emits `property.check.start` once per *property* with `index`+`detail`, Core once per *run* with `unit_id`; orchestrator's `property.verdict` uses `verdict=`, Core's uses `outcome=`; and the two orchestrator verdict sites disagree with each other (`:331-336` carries no `k`). Core also emits verdicts *after* the store closes, throwing away the incrementality `orchestrator/check.py:341-345` and `orchestrator/loop.py:102-108` were built for (#100), so a `forseti check` killed mid-ladder writes zero `property.verdict` lines.
- **First seen**: 2026-09-21
- **Reason**: the valuable version — one emitter owning both, per-property — changes `property.check.start` **cardinality in a published wire format**, so it is a human decision, not an unattended run. The behaviour-preserving half is narrower than "just inject a sink": give the event-name vocabulary one stdlib-only leaf both layers may import (layering runs Core → orchestrator, so there is none today), and separately move Core's verdict emission behind an injected sink with identical fields. Test-first precondition: **nothing** asserts the Core-side shape — `tests/core/test_core_check.py` never references `events.jsonl`, `events_path`, `PROPERTY_VERDICT` or `PROPERTY_CHECK_START` — so those lines execute under the coverage gate while being unasserted; pins must land first.

## signature-tests-two-homes

- **Status**: proposed
- **Score**: 15/25 (leverage 2, locality 3, blast radius 1, heat 3)
- **Files**: ~1 estimated
- **Modules**: `tests/properties/test_signature.py:31-145`, `tests/properties/test_harness.py:464-514` and `:796-823`
- **Summary**: Nine test names exist in both files; five are byte-identical duplicates and four differ only in fixture. The copies have drifted — `test_signature.py` carries 8 cases `test_harness.py` lacks — so a reader asking "where is `extract_signature` pinned?" has to check both and reconcile. Deleting the nine from `test_harness.py` is safe: `test_signature.py` is a strict superset and `test_signature.py:151` already pins `harness.extract_signature is signature.extract_signature`.
- **First seen**: 2026-09-21
- **Reason**: leverage 2 — test hygiene, not module depth; it buys no interface change. Cheapest entry in the backlog if someone wants a warm-up.

## orchestrator-run-record-append

- **Status**: proposed
- **Score**: 13/25 (leverage 2, locality 3, blast radius 2, heat 2)
- **Files**: ~3 estimated
- **Modules**: `src/forseti/orchestrator/persistence.py:188-210` (`persist_run`), `:213-236` (`persist_property_check`), `src/forseti/orchestrator/__init__.py:19`, `:72-73`
- **Summary**: The two functions are the same eight-line JSONL recipe — `_unit_slug` → `root/<subdir>/<slug>.jsonl` → `mkdir(parents=True, exist_ok=True)` → a three-key record → append one `json.dumps(...) + "\n"` → return `dest` — differing only in subdirectory (`runs/` vs `property-checks/`), body key (`"report"` vs `"run"`) and projection. **But neither has a caller in `src/`**: grepping `src`, `tests`, `demo`, `examples` and `adapters`, the only non-test references are the `orchestrator/__init__.py` re-export and `__all__` entries.
- **First seen**: 2026-09-21
- **Reason**: weak deletion test, stated honestly — collapsing the two concentrates the JSONL shape, but it is ~8 lines × 2 with no production pressure to drift, and deleting the module outright would *remove* complexity rather than concentrate it (the same reasoning `harness-writer-port-inline` and `verify-and-record-decomposition` were dropped on). The honest move is to put the **dead-surface question** to a human first — is `.forseti/runs/` still a live #15 artifact, or did the Core faces supersede it? — and only then decide whether to merge or delete.

## event-log-write-text-atomic-reexport

- **Status**: dropped
- **Score**: — (leverage 1)
- **Files**: n/a
- **Modules**: `src/forseti/adapters/claude_code/event_log.py:32`, `forseti_gate.py:1815`, `claude_code/install.py:229`, `:266`; `tests/adapters/claude_code/test_event_log.py:48`, `:62`, `:68`
- **Summary**: `event_log.py:32` re-exports `write_text_atomic`, and two of five callers reach the filesystem primitive *through the trace module* while three import `_atomic` directly; the primitive's tests also live in the trace module's test file.
- **First seen**: 2026-09-21
- **Reason**: Leverage 1 — a one-line import fix plus moving three tests. Import hygiene; nothing concentrates and no interface gets deeper. Same filter as `signature-reexport-indirection`.

## esbmc-result-render-runner-cluster

- **Status**: dropped
- **Score**: — (leverage 1)
- **Files**: n/a
- **Modules**: `src/forseti/esbmc/result.py`, `render.py:31`, `:58`, `runner.py:66`, `counterexample.py`, `cex_parser.py`
- **Summary**: The five small `esbmc/` result modules look shallow by line count and invite a "collapse them" candidate.
- **First seen**: 2026-09-21
- **Reason**: Leverage 1 — fails the deletion test. `render.py:31` and `:58` are exhaustive `match`es over a sealed union with `assert_never`, and `runner.py:66` `classify` is a genuine abstraction over esbmc's banner grammar; collapsing them scatters complexity into callers rather than concentrating it. Recorded so a later run does not re-derive it.

## Run log

### Run 2026-09-18 — complete

- **Outcome**: complete
- **Stopped at**: step 6 — PR #294 opened; work landed on branch
- **Branch**: `pm-deepen/precond-sidecar-run-seam` — *created* as `pm-deepen/run-2026-09-18-0102` from `origin/main` and renamed at step 2. Branch adoption was **refused** at step 0 on condition 3: the firing branch (`sym/forseti/routine/refactor-audit/01M2RSMHC9`) had an upstream (`@{u}` resolved to `origin/main`), so it was not a made-for-this-run, no-upstream branch.
- **Committed**: review report + design pass (winner: Design C — `SidecarRunner` binding work_dir/max_len/ladder_cap/raw with `climb`+`probe` and a `sidecar_runner` context manager; runner-up design B — the plan-binding, `attempts`-exposing variant — lost on seam placement, since discharge re-plans per caller; B's `ProbeSite` enum was adopted into C), the `refactor(precond)` implementation (new `precond/run.py` + `tests/precond/test_precond_run.py`, verify/discharge rewired, `core/check.py` import retargeted), and this backlog update.
- **Evidence**: quality gate green — ruff check + ruff format --check + ty check + pytest (1686 passed, 1 skipped, ESBMC-gated included, esbmc 8.3.0 on PATH); project coverage 97.99% (gate 96%), `precond/run.py` 100%; PR #294. Diff **5 files** vs the ~4–6 estimate. Behaviour preservation proved by `tests/precond/test_precond_verify.py` and `tests/precond/test_discharge.py` passing **untouched** (both dispatch canned verdicts by sniffing the emitted harness filenames). The escalate-vs-raw test was **mutation-checked**: routing `probe` through `escalating_port` fails exactly that one test and no other, while both driver suites stay green — they ignore `unwind` entirely.
- **Next**: human review of PR #294 (do not merge as part of the routine). Natural next firing: the runner-up candidate `forseti-cli-json-subprocess-seam` (20/25, within 1 point) — it also carries a **live bug** for a human, independent of the refactor: the Codex and Oh-My-Pi hooks forward no `FORSETI_BUILD_FLAGS`, so on any project with `-I`/`-D` they report `skipped`/`error` where the Claude gate gets a real verdict. Note its argv half is unpinned at two of three harnesses (both suites stub `fake_run(*_a, **_kw)` and ignore their arguments), so argv-shape assertions must land before that extraction.

### Run 2026-09-21 — complete

- **Outcome**: complete
- **Stopped at**: step 6 — PR #307 opened; work landed on branch
- **Branch**: `pm-deepen/cli-command-trace-wrapper` — *created* as `pm-deepen/run-2026-09-21-0102` from `origin/main` and renamed at step 2. Branch adoption was **refused** at step 0 on condition 3: the firing branch (`sym/forseti/routine/refactor-audit/01M30GVV07`) had an upstream (`@{u}` resolved to `origin/main`), so it was not a made-for-this-run, no-upstream branch.
- **Committed**: review report + four-design pass + adjudication; a standalone `test(core)` commit adding the `cli.command` key-set pins to the two existing trace tests **before** anything moved; the `refactor(core)` implementation (new `core/_cli_trace.py` + `tests/core/test_core_cli_trace.py`, `_precond_cli`/`cli` rewired onto the seam, `events.py` policy comment and the design-doc row updated); and this backlog update.
- **Evidence**: quality gate green, each step a separate command — ruff check + ruff format --check + ty check + pytest (**1718 passed, 1 skipped**, ESBMC-gated included, esbmc 8.3.0 on PATH); project coverage **98.00%** (gate 96), `core/_cli_trace.py` **100%**; PR #307. Diff **8 files** against the ~4–5 estimate (6 in the refactor commit + 2 test files in the pin commit) — inside the 2x bail threshold but at its edge, and stated in the PR body rather than left for a reviewer. **Wire format proved byte-identical, not assumed**: a baseline worktree at the pre-refactor commit and this tree were each run over the two trace test files with the same `--basetemp`, every emitted `cli.command` record captured with `ts`/`duration_s` stripped — 36 records each side, `diff` empty. The new key-set pin was **mutation-checked**: adding one field to `_precond_cli`'s `record_event` call fails all six parametrisations and nothing else. `ty`'s behaviour on the PEP 695 generic was **probed empirically first** (the repo has no generics today): ty 0.0.81 joins `ResultT` across both positions but still rejects a cross-wired `fields` builder, so no `TYPE_CHECKING` pairing guards were needed.
- **Design**: winner **Design D** (a `traced[ResultT](command, run, args, *, fields)` function below both CLI modules) **with both of its ports deleted** — D declares `ClockPort` hypothetical outright and rates `TracePort` only "weakly real", and Designs A and C each argue independently that injecting the sink makes the tests *weaker* (a fake stops exercising `sort_keys`, the one-line append and JSON-serialisability). Runner-up design **A** (`@traced("synth")` over a closed `Literal` command set with `assert_never`) won depth but lost locality: its trace module must import `Assessment` *and* `SemanticLoopResult`, so `_precond_cli` would transitively gain `core.loop` → `orchestrator` → `properties` in a repo that already keeps CLI hook imports deliberately lazy (`cli.py:925-929`).
- **Next**: human review of PR #307 (do not merge as part of the routine). Natural next firing: the runner-up candidate `cli-check-phase-argument-block` (20/25) — one file, argparse surface unchanged, both parsers already exercised through `cli.main`, and the lowest blast radius in the backlog. Three findings surfaced this run that are **bugs for a human, not refactors**: (1) `render_semantic_harness` emits C naming an undeclared identifier instead of raising `HarnessError` (`renderability-authority-misses-identifiers`, verified by running it); (2) `forseti synth|discharge --emit-only -t N` silently ignores `-t` and probes with `list_units`' own 30 s default (`precond-unit-lister-default-four-copies`); (3) `adapters/claude_code/post_bash.py` is the only one of five gate emitters that emits no canonical `gate.decision`, so a C file written out-of-band via Bash and blocked is invisible in the cross-harness trace (recorded on `canonical-gate-decision-helper`).
