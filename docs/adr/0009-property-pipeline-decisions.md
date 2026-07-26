# ADR-0009 — Property pipeline: store, scope, transport, verdict

- **Status:** Accepted
- **Date:** 2026-07-03 (decided when W2 was decomposed into #62–#66)
- **Recorded:** 2026-07-26 — written retroactively per #103, from the decision comment on
  epic #3 and the four sub-issue bodies (#62 D1, #63/#64 D2, #65 D3, #66 D4). No decision is
  changed here; this file makes the already-implemented record greppable.

## Context

Epic #3 (W2 — property generation) was decomposed into #62 → #66 on 2026-07-03. Four questions
gated that decomposition, each one deciding what a sub-issue could even be scoped to: where
proposed properties live, whether reachability properties are checked in W2, who makes the LLM
call, and what a checked property produces. They were taken together as one batch of *velocity*
choices for P2, and the code cites them as **ADR-0009 D1–D4** — 22 citations: 21 in `src/`
(`properties/`, `orchestrator/`, `core/propose.py`) and one in `tests/properties/test_model.py`.

## Decision

### D1 — Store: SQLite at `.forseti/forseti.db`

Proposed properties live in a **SQLite** database at `.forseti/forseti.db` (gitignored, stdlib
`sqlite3`), keyed at unit level (`path::symbol`), with `add / get / list-for-unit /
update-status`.

This **supersedes the SQLite deferral** in `orchestrator/persistence.py` ("deferred until a
query workload actually needs it"): per-unit lookup with lifecycle status updates, later joined
with grading verdicts (#4), *is* that workload. The two coexist — JSONL under `.forseti/runs/`
stays the append-only run trace; the database is the queryable property state.

### D2 — Reachability: `--error-label` is the primitive, emission is deferred

The engine primitive for a reachability property is ESBMC's **`--error-label`** on
caller-inserted labels (not the `__ESBMC_unreachable()` source intrinsic), so a harness needs no
dependency on the unit's source.

**Emitting those labels is out of scope for now.** W2 ships *semantic* properties only. #63
(runner support) is parked and no longer blocks #64; the harness writer renders semantic
`assert(...)` and refuses a reachability property; the check driver skips one.

Reachability stays *representable* in the model (`PropertyKind.REACHABILITY`) so the store and
the wire shape do not need a migration when it is unparked — it is inert, not absent.

### D3 — Proposer: a Core-native LLM call behind an injected client

The proposer calls the model **from Core**, not by delegating the call to the host harness, so
the grading loop (W3, #4) and GEPA (#5) can drive it programmatically. This settles #44's
wiring: Core owns the call and the CLI/MCP `propose` face is a thin wrapper over it.

For v1 the backend is **hardcoded to `claude -p`** (`ClaudeCliClient`), injected through the
provider-agnostic `LLMClient` port so tests stay hermetic and a second provider is a new
implementation rather than a rewrite. Generalising the provider is deliberately later work.

### D4 — Check: one verdict per property, and nothing more

The W2.5 loop produces a **verdict per property — `held / violated / unknown`** — persisted and
surfaced through the existing telemetry/transcript.

Mutation and kill-rate *scoring* stay in grading epic #4; this decision only reserves the field
they populate. Note the vocabulary: `held` is the property-level analogue of `VERIFIED`, i.e.
*no violation found up to bound k* — not a proof.

## Consequences

- **The store is a real dependency of the pipeline, not a detail.** `properties/store.py` opens
  `.forseti/forseti.db` directly; anything that needs property state goes through
  `PropertyStore`, not through the JSONL trace.
- **No new runtime dependency.** `sqlite3`, `json` and `subprocess` are stdlib, so D1 and D3
  both hold `dependencies = []` and keep the base install and the loop/esbmc path
  dependency-free.
- **D2 costs an inert enum arm and buys a shipped W2.** The price is dead-but-typed code paths
  (`HarnessError` on a reachability property, `PropertyOutcome.SKIPPED` in the check driver)
  that must stay honest: a deferred kind is *skipped and reported*, never silently coerced into
  a code verdict.
- **D4's triad is the grading verdict, not the whole outcome set.** `PropertyOutcome` adds
  `error` (a tooling/invocation failure, e.g. a harness that did not compile) and `skipped`
  (D2's deferred kind) alongside the three, precisely so neither can masquerade as `held`.
  `UNKNOWN` stays a distinct state per the CLAUDE.md loop policy.
- **D3 concentrates the blast radius of a provider swap** in one class. It also means the
  proposer path is the one place in Core that shells out to an LLM, so its failure mode is
  fail-loud (`LLMError`) rather than "propose nothing."
- The four are **velocity choices for P2**, not permanent architecture — see below.

## Revisit

These were taken as provisional, to be re-examined at the **P2 grading-cost checkpoint**
(`docs/roadmap.md`, P2): D1's backend, D3's hardcoded `claude -p`, and D4's verdict-only scope
are all expected to be revisited once grading cost is measured. D2 is unparked by prioritising reachability
properties (#63).

ADRs are immutable once Accepted: any of these changing means a **new superseding ADR**, not an
edit here.

## References

- Epic #3 (decision comment, 2026-07-03) and sub-issues #62, #63, #64, #65, #66; #44.
- `src/forseti/properties/{store,model,llm,harness,proposer,prompts}.py`,
  `src/forseti/orchestrator/{check,persistence}.py`, `src/forseti/core/propose.py`.
- ADR-0003 (C first — W2 harnessing is C only), ADR-0005 (why the rationale lives here and the
  tracking lives on GitHub).
