# Design RFC 0006 — Positioning after the Oct-2026 verification papers: specs from existing artifacts

- **Status:** Draft / RFC (thinking aid — not yet an ADR)
- **Date:** 2026-10-23

## Problem

Two October 2026 results change the landscape Forseti was planned against in Q1:

- **Paper A** — Anthropic's formal-verification research post
  ([anthropic.com/research/formal-verification](https://www.anthropic.com/research/formal-verification)).
- **Paper B** — Ye, Ghai, Qin, Song, Kong (UC Berkeley / AWS), *Agentic Verified Optimization of
  Real-World Software* ([arXiv:2610.30188](https://arxiv.org/abs/2610.30188)).

Together with veripp (an ESBMC+LLM tool for C/C++, already public), they make the
"LLM + checker finds memory-safety bugs in existing C" pitch crowded at both ends: cheap and
fast (veripp), expensive and unbounded (Paper A). This RFC asks what Forseti should claim to be,
and which W2–W4 design choices those papers should change *before* W3/W4 are built.

## What the papers did

**Paper A (Rocq, binary level).** Existing OSS-Fuzz harnesses are compiled to RISC-V and turned
into a spec: the entry point does not crash for any input under 64 MB. Claude writes Rocq proofs
and the Rocq kernel checks them. Headline: *"no crashes on any input (up to 64 MB) for 239 entry
points across 123 different OSS-Fuzz libraries"*, ~180 million lines of Rocq, a typical proof
~600,000 lines; *"the proofs can take hundreds of millions of tokens to produce."* Proof attempts surfaced
real bugs (including a rediscovered libexpat CVE). Scope limits stated by the authors:
*"single-threaded, static RISC-V binaries linked against musl libc."* They explicitly avoid
*"the need to rewrite entire codebases in another language"*, and name the risk Forseti also
faces: *"Claude could make any theorem provable by rewriting the program into one that trivially
never crashes"* — handled by penalising patches and auditing them by hand.

**Paper B (Lean 4, source level).** An agent rewrites a function `f` into a faster `f′` together
with a Lean proof that `∀x, f x = f′ x`. The **existing implementation is the spec**, so no
spec is written. To get real programs into Lean, the AQUEDUCT framework ports C/C++/Go/Rust
programs; each port is validated only by differential testing. Results (Codex, GPT-5.6 Sol):

| Setting | Result |
|---|---|
| 189 Verina functions | 156 with a faster proved rewrite; geomean 1.851×, median 1.117× |
| 17 ported repos, function level | 92 proved rewrites; geomean 1.864× over the Lean port |
| Same, application level (51 scenarios) | faster in 47/51; geomean 1.120× |
| Lean port vs. original native program | 0.126× geomean (optimised: 0.235×); only 15/92 functions beat the original |
| Port fidelity (hidden ProgramBench tests) | 87.68% recovery vs. 68.17% for a single agent |

Their ablations (Appendix C) measure what made the porting pipeline work: candidate diversity
(−14.72 points without it), mutation feedback in test generation (−10.05), coverage feedback
(−9.80), repeated repair (−8.46). A *fixed phased pipeline* scored 53.32% — below the single
agent. Their conclusion: *give agents freedom over how they work and concentrate rigour in the
acceptance criteria.* Out of scope: floating point (only a tolerance on output formatting), I/O
and other opaque effects; "equal within a relation R" is listed as future work, and so is
certifying the migration itself.

## What they share — and where each one's trust gap sits

1. **Neither writes a spec.** A reuses fuzz harnesses; B reuses the reference implementation.
   W2 took the hardest road (LLM-proposed semantic properties) and has no cheaper fallback.
2. **The checker owns acceptance.** B's acceptance checker (Appendix A) rebuilds in a fresh copy,
   requires the reference byte-identical, whitelists axioms, forbids `sorry`/`native_decide` and
   compiled-code substitution. A audits patches.
3. **Both name reward hacking** as the open problem once the agent may edit the code under check.
4. **The trust gap moves to the edges.** For A it is the spec ("formally verified" ≠ bug-free).
   For B it is the migration (differential testing only). Porting — to Lean or to Rust — does
   not remove the obligation; it moves it into the translation. For Lean it also cost ~8× in
   runtime on B's own numbers.

## Proposed positioning

Forseti should stop pitching "ESBMC for C" and pitch **spec quality inside the agent loop**:

- **Cost/assurance tier.** ESBMC returns a verdict plus a concrete counterexample in seconds on
  every edit; A spends hundreds of millions of tokens per entry point offline. Different tiers,
  not competitors: Forseti is the per-edit gate, proof campaigns are the certification tier.
- **Native semantics.** Forseti checks the C that ships, with ESBMC's C memory model and
  bit-precise IEEE-754 — the area B excludes and where Lean's verified floating-point libraries
  are still immature. No migration gap.
- **The backend-independent contribution is grading specs.** Mutation-kill (W3) answers *"is
  this spec strong enough to trust its VERIFIED?"* — the gap A concedes. The grading does not
  depend on ESBMC; it could score specs written for Rocq or Lean.
- **Already distinctive:** compositional precondition discharge (RFC-0003). B's guarded
  specialisation — `∀x, P x → f x = g x`, deploy `g` alone *"if the callers guarantee P"* — is
  exactly that problem. Today Forseti discharges *memory* preconditions at every caller in the
  same TU, with a site probe against vacuous discharge and named failure states for what it
  cannot discharge; extending that mechanism to semantic domains is open work.

Vocabulary stays strict (CLAUDE.md): "proof" only for A's and B's kernel-checked results; ESBMC
gives "VERIFIED up to k".

## Proposal

### 1. Three property sources, routed by edit intent

| Source | What it checks | Status |
|---|---|---|
| **S0 — built-in safety** | memory safety, overflow, bounds, UB via `--function` | landed (verify gate) |
| **S1 — equivalence to a reference** | `old(x) ≡ new(x)` up to k | **new** (this RFC) |
| **S2 — proposed semantic properties** | LLM pre/postconditions, graded by mutation-kill | W2 landed; W3/W4 open |

S1 applies to edits meant to preserve behaviour (refactor, optimise, port within C). Bug fixes
and features change behaviour by design, so they keep needing S2. This is the concrete
"trusted baseline" that #182 asks for before a property becomes a code-fix obligation.

### 2. The equivalence harness (S1)

One TU holds the reference (renamed, e.g. `f__ref`) and the candidate `f`; `main` draws nondet
inputs once, hands **each version its own copy** of every input buffer, calls both, and asserts
the relation over the return values *and* every output buffer. Inputs are limited to what
`renderability_reason` in `properties/harness.py` already accepts: scalars, `(ptr, len)` buffers
under `--max-len`, and scalar-backed outputs.

- **Relation, not `==`.** For floats `==` is wrong: NaN ≠ NaN gives a spurious VIOLATED and
  −0.0 == +0.0 hides a real difference. Default to bitwise equality (or "both NaN, or equal");
  ε-equivalence is an explicit opt-in relation, checked bit-precisely by ESBMC.
- **Verdicts.** VERIFIED = no divergence found up to k. VIOLATED = a **behaviour-diff witness**
  (input, old output, new output). UNKNOWN = raise k or report, per CLAUDE.md.
- **Intent decides blocking.** On a declared behaviour-preserving edit a VIOLATED blocks. On a
  bug fix it is the *expected* result and a useful review artefact — "the fix changes behaviour on
  exactly these inputs" — reported, not blocking.
- **Guarded form.** `P(x) → f__ref(x) ≡ f(x)` for specialisations. `P` must be checked for
  vacuity (Risk 4) — an unsatisfiable `P` is trivially VERIFIED — and should be discharged at
  callers by generalising RFC-0003 S3's injected-obligation mechanism beyond `__ESBMC_r_ok`.
- **Model change.** An `EQUIVALENCE` `PropertyKind` is additive under ADR-0009 (like the inert
  `REACHABILITY`), not a supersession.

### 3. Reuse inside W3 (#4)

- **Equivalent-mutant filter** (Risk 3, an open box on #4): skip a mutant that S1 finds equivalent
  to the original up to k. Known bias: a mutant that diverges only beyond k is skipped too,
  shrinking the denominator and *inflating* kill-rate. Report the number skipped.
- **Mutation as feedback, not only a score.** Feed surviving mutants back to the proposer
  ("propose a property that kills this mutant"), as B does for test generation (−10.05 points
  without it).

### 4. Diversity and held-out evidence in W4 (#5)

- A **portfolio of proposer strategies** (different prompts or input recipes), selected by
  kill-rate, before evolving any one of them — B's largest single gain.
- A **held-out split** of kernels *and* mutants that GEPA never sees, so the reported v1→v2 gain
  measures generalisation (already an exit criterion of P3; this makes the split concrete).
- No agent grades its own work: the proposer never sees the held-out mutants.

### 5. Acceptance integrity (audit, not a claim)

Core renders harnesses, which is good. Open: whether the gate pins the property store
(`.forseti/forseti.db`, a file on disk) and the reference against edits by the gated agent, and
whether ESBMC flags are Core-owned. B's Appendix A is the checklist to audit against. #266
(withdraw/reclassify a property) needs this answered first: a withdraw the gated agent can call is
the escape hatch A warns about.

### 6. Writeup (W7)

Lead with spec quality, not with "ESBMC for C". Report mutation scores B's way: bootstrap 95% CI
(B: 53.2%, CI 39.1–67.0%) and a paired test against a baseline suite. Report UNKNOWN and
skipped-mutant counts beside every kill-rate (Risk 9).

## Non-goals

- **No pivot to Lean/Rocq** (ADR-0007 unchanged) and **no port to Rust**.
- **No cross-language translation validation.** ESBMC cannot read Lean or Rust; S1 is within C
  (later C++). VERT (cited by B as BMC-checked translation to Rust) is related work to read.
- **No "proof" claims** for S1: equivalence is "up to k".

## Sequencing — the honest calendar

As of today P3 has no closed issues, and #4 and #5 have no sub-issues. A positioning built on
W3/W4 rests on unbuilt work, so the order matters:

1. **S1 equivalence harness** — small, reuses `harness.py`, and also delivers #4's
   equivalent-mutant filter.
2. **Minimal kill-rate scorer** (#4) with mutation feedback to the proposer.
3. **Proposer portfolio + held-out split**, then GEPA (#5).

This keeps ADR-0006's intent (de-risk the novel part on kernels before real code) but may shift
P3/P4 dates; if it reorders ADR-0006, that needs a superseding ADR, never an edit. ADR-0002
(success metric), ADR-0003 (languages) and ADR-0007 (Lean off critical path) are unchanged.

## Open questions

1. **Reference choice for S1:** last gate-verified snapshot, git `HEAD`, or a user-chosen ref?
2. **Who declares intent** (behaviour-preserving vs. behaviour-changing): the user, the agent, or
   inferred from the task — and can the gated agent change it?
3. **Callees and statics:** when the caller and a callee both change, compare against which
   callee? How are `static` and name collisions handled when both versions share one TU?
4. **Global state:** compare globals written by both versions, or exclude stateful units from S1?
5. **Proposer inputs and outputs** — what Forseti sends the S2 proposer and what shape it returns
   (contracts, relational/two-call properties, pre-state references, loop invariants) is the
   central experimental question and gets its own RFC.

## References

- Anthropic, *Formal verification* (Oct 2026): https://www.anthropic.com/research/formal-verification
- Ye et al., *Agentic Verified Optimization of Real-World Software*, arXiv:2610.30188 (Oct 2026)
- #182 (Specula implications: when a property becomes a fix obligation), #266 (withdraw command),
  #4 (W3), #5 (W4); roadmap Risks 3, 4, 9; RFC-0002, RFC-0003; ADR-0006, ADR-0009.
