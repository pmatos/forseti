# Forseti Roadmap — H2 2026

**Window:** Jul 2026 → Jan 2027 · full-time (~35 h/wk), agent-amplified.
**Codename:** Forseti ([ADR-0001](adr/0001-codename-forseti.md)).

This is the narrative spine. Live task tracking is on GitHub (Milestones = phases,
Epic issues → sub-issues = drill-down, Project board = kanban). Decisions live in
[`adr/`](adr/). When the GitHub repo exists, each epic below links to its issue number.

---

## North star

> As agents write faster than anyone can review, trust moves from *"a human looked at it"*
> to *"the code carries a proof."* Forseti puts the ESBMC verifier **inside** the agent loop.

**Success metric (the bar):** a *demoable* write → verify → counterexample → fix loop that
self-corrects on a **non-toy** example, plus a public writeup (blog / paper / talk).
ESBMC fixes and a Lean backend are *supporting*, not the bar
([ADR-0002](adr/0002-scope-and-success-metric.md)).

**Languages, in priority order:** C → C++ → Python ([ADR-0003](adr/0003-language-priority.md)).

**Explicitly out of scope:** Vow, the proof-native language — the bigger, separate bet
([ADR-0008](adr/0008-vow-out-of-scope.md)).

---

## Workstreams

| # | Stream | Lives in | Notes |
|---|---|---|---|
| W1 | **The Loop** (orchestrator) | `src/orchestrator/` | The spine: write→verify→cex→fix |
| W2 | **Property generation** | `src/properties/` | LLM proposes semantic + reachability properties |
| W3 | **Differential grading** | `src/grading/` | Mutation-kill scoring — the novel contribution |
| W4 | **GEPA** | `src/gepa/` | Evolve the property-proposing prompt |
| W5 | **ESBMC hardening** | fork of `esbmc/esbmc` | Continuous; fork now, upstream in batches ([ADR-0004](adr/0004-esbmc-fork-strategy.md)) |
| W6 | **Real-code retrofit** | `examples/` | Push loop onto real C/C++ |
| W7 | **Writeup / evangelism** | `docs/writeup/` | Blog → paper/talk; the public half of the bar |
| W8 | **Lean stretch** | TBD | Unbounded 2nd backend; off critical path ([ADR-0007](adr/0007-lean-off-critical-path.md)) |
| W9 | **Harness integration** | Core + adapters | Neutral Forseti Core (CLI + MCP) + Claude Code / Codex / opencode adapters; forked `esbmc-plugin`. See [RFC-0001](design/0001-harness-portability.md) |
| W10 | **Observability** | Core | Structured JSONL event trace of the whole protocol (triggers, Core calls, ESBMC verdicts, counterexamples, fixes). Debuggability from day one. See [RFC-0001](design/0001-harness-portability.md#observability-required-from-day-one) |

**Sequencing rule:** property-gen + GEPA (W2–W4) are de-risked **before** the real-code +
ESBMC push (W5–W6) — lock the novel contribution on controllable kernels first
([ADR-0006](adr/0006-sequencing-gepa-before-real-code.md)).

**W9 phasing:** the neutral Core (CLI + MCP) + the Claude Code adapter land alongside the loop
in **P1** (we develop the loop *inside* a harness from the start); Codex and opencode adapters
are **P4–P5** once the loop is proven. Design open questions are in [RFC-0001](design/0001-harness-portability.md).

**Design RFCs:** [`docs/design/`](design/) — strawmen under discussion before they become ADRs.

---

## The 6 phases

### P0 · Jul — Foundation
Stand up the repo and the smallest end-to-end slice.
- **Exit:** ESBMC is callable programmatically; its output parses into a typed
  `VERIFIED | VIOLATED+counterexample | UNKNOWN`; **one manual** write→verify→cex→fix turn
  completed by hand on a C kernel (e.g. `abs`/`INT_MIN`, or a bounded ring buffer).

### P1 · Aug — The Loop
Automate the spine.
- **Exit:** the orchestrator closes write→verify→cex→fix **without human intervention** on a
  set of C kernels, with a max-iteration give-up policy and clean handling of `UNKNOWN`.

### P2 · Sep — Properties v1 + grading harness
Stop hand-writing properties; start scoring them.
- LLM proposes candidate properties (semantic **and** reachability) into a property store.
- Build the **differential mutation-kill** grading harness: mutate the code, run ESBMC,
  score = "holds on real code AND breaks on mutants."
- **Exit:** for a kernel, the harness emits a numeric kill-rate + plain-English reason per
  property; the loop consumes generated (not hand-written) properties.
- **Checkpoint:** measure grading cost for one program → decide compute strategy
  (see Risk 6; candidate mitigation = stateful ESBMC).
- **Touchpoint:** ~30-min Lean feasibility chat with Jesse (W8 scoping).

### P3 · Oct — GEPA
Make the proposer good, automatically.
- Wire GEPA to rewrite the property-proposing prompt against the kill-rate score; keep the
  Pareto-best variants.
- **Exit:** a measured v1→v2 improvement in mutation-kill rate on a held-out kernel set.

### P4 · Nov — Real code + ESBMC push
Leave the sandbox.
- Run the loop on real C (then C++); land ESBMC fixes on the fork to unblock targets;
  upstream a curated PR batch.
- **Exit:** the loop completes on ≥1 real-world C module; **stretch:** a genuine bug caught
  with a counterexample.

### P5 · Dec–Jan — Harden + writeup + Lean
Make it solid and tell the story.
- Stabilize the live demo; write the blog/paper/talk; explore proof-carrying packaging.
- **Lean spike** only if Jesse committed in Sep.
- **Exit (the bar):** clean live demo on a non-toy example + published writeup.

---

## Risk / corner-case register

| # | Risk | Mitigation |
|---|---|---|
| 1 | ESBMC returns **UNKNOWN** (timeout / k too small) | Treat as a distinct loop state — raise k, simplify harness, or report. Never silently pass. Prefer `--unwind N --no-unwinding-assertions` over `--incremental-bmc` (known to yield UNKNOWN on small unwind). |
| 2 | Loop **never converges** (agent keeps failing to fix) | Hard max-iteration cap → give up and report to human with the last counterexample. |
| 3 | **Equivalent mutants** (behavior-preserving) can't be killed | Detect/skip; don't penalize a property for surviving them. |
| 4 | **Vacuous / unreachable** properties pass trivially | Reachability check before trusting a VERIFIED; ties into the reachability-property work. |
| 5 | **Counterexample format drift** across C / C++ / Python frontends | Frontend-aware parser; cover each language's cex shape with fixtures. |
| 6 | **Grading-compute blowup** (programs × properties × mutants) | Measure in P1 first. Candidate mitigations: result caching, mutant caps, cluster batching, and a **stateful ESBMC** that amortizes many properties over one module. |
| 7 | **ESBMC breakage** on real code (segfaults, incomplete `std::string`/stdlib models) | The W5 fork-and-fix stream; upstream batches. |
| 8 | **External dependency on Jesse** for Lean | Off critical path; Sep scoping chat; metric never depends on it. |
| 9 | **Over-claiming soundness** in the writeup | Be explicit: bounded = "proven up to k," not for all inputs. Honesty is the deck's whole credibility. |
| 10 | **Scope creep into Vow** | Guardrail: Vow is out ([ADR-0008](adr/0008-vow-out-of-scope.md)). |
| 11 | **Upstream review latency** (Lucas / maintainers) | Fork-now / upstream-in-batches keeps the loop unblocked. |

---

## Open questions (from the deck — research, not committed deliverables)

- **Proof-carrying packaging:** ship agent-written code *with* the properties it satisfies.
- **Provability gaps:** aim generated tests at exactly where bounded proof runs out (past k).
- **PBT crossover:** turn generated properties into property-based-testing generators + oracles.
