# Design RFC 0007 — What should we prove? The property proposer at project scale

- **Status:** Draft / RFC (thinking aid — not yet an ADR)
- **Date:** 2026-10-23

## Problem

The property proposer decides *what* Forseti checks, and so it decides how much a VERIFIED is
worth. Two questions are open and should be answered by measurement, not taste:

1. **What does Forseti send the proposer?** The whole function, an interface, a description,
   a formal spec, the surrounding context — and which of these actually matter?
2. **What does the proposer send back?** Pre/postconditions, intermediate asserts, loop
   invariants, something else?

The target setting is **large existing C projects** (think the Linux kernel), where an agent
adds a new function to an existing file or modifies an existing function. Single-file kernels
are a smoke test, not the goal. This RFC designs the experiments and tooling to answer both
questions in that setting. It builds on RFC-0006 (spec quality as the positioning; equivalence
as property source S1).

## Where it is today

`semantic/v1` (`properties/prompts.py`) sends the **whole unit file** plus the symbol name
(`core/propose.py` reads the file as-is). It gets back up to 6 candidates, each
`{expression, domain, referenced_params, rationale}`: one postcondition after a single call,
plus preconditions over inputs only. Harnesses (`properties/harness.py`) take scalars,
`(ptr, len)` buffers under `--max-len`, and output buffers. They cannot express pre-state
("an in-place sort is a permutation of its input"), properties over two calls, intermediate
asserts, loop invariants, or invariants of struct arguments. `Provenance` records
`prompt_id`, `prompt_version`, `provider`, `model`.

## Ground rules

### 1. Temporal hygiene: only artifacts that existed when the code was written

Our own corpus leaks the answer to the proposer in three ways:

- every `examples/*.c` has a harness `main` whose `assert`s *are* the target properties;
- file headers describe the intended properties and the latent edge case, and the `_bug`
  twins name the staged defect (*"rb_push drops its `if (r->count == CAP)` full-check"*);
- `semantic/v1` uses `abs` as its few-shot example, so `abs.c`/`abs_fixed.c` are contaminated.

The general rule is not "strip comments". In a real project, comments such as kernel-doc are
genuine author intent and are exactly what the proposer should read. The rule is: **the context
may contain only artifacts that existed at write time.** Leaks are (a) annotations written by
us with knowledge of the answer, and (b) in history replay, anything written after the change —
the fix commit, its message, later tests, later comments. Use the *introducing* commit's code and
message, never the fix's.

### 2. Kill-rate alone rewards transcription

A proposer that reads the body and restates it as a postcondition kills nearly every mutant —
and, if the original has a bug, encodes the bug. So mutation-kill cannot be the primary metric;
**bug-pair discrimination** is (see Metrics).

### 3. A hold counts only under the verification discipline

`--unwind k --no-unwinding-assertions` gives a spurious VERIFIED for post-loop asserts when `k`
does not exceed the trip count (`examples/README.md`, roadmap Risk 1). A property "holds" only
with `k` above the trip count *and* a reachability/non-vacuity check passed. This is part of every
metric's definition, not a separate one.

## The unit of experiment: an edit event

An **edit event** is: the project snapshot at the parent commit, the diff, and the intent text
(task prompt, issue, or commit message), mapped to the functions it touches.

| | Modified function | New function |
|---|---|---|
| Previous version | yes → S1 equivalence (RFC-0006) | no |
| Accepted properties | carried forward and re-checked | none |
| Proposer's job | properties for *what changed* | properties from scratch |
| Strongest intent sources | diff, intent text, behaviour-diff witness | intent text, callers, neighbours, analogues |

For a modified function, the S1 behaviour-diff witness (input, old output, new output) is itself
a proposer input: "these inputs changed behaviour — state the new intended behaviour." Renamed,
moved and deleted functions are out of scope for v1 (see open questions).

## Inputs: a context pack, not the whole file

Whole files do not scale (kernel files run to thousands of lines) and mix intent with noise.
The proposer instead gets a **context pack**: a ranked, token-budgeted set of slices drawn from a
project index at the parent commit.

| Slice | Content | Hypothesis |
|---|---|---|
| **Target** | the function (post-edit) and its doc comment | grounding: names, domains, overflow sites |
| **Intent** | task prompt / issue / commit message | the strongest body-independent source |
| **Diff** | old vs. new, for modified functions | focuses on what changed |
| **Types** | struct/typedef/enum/macro definitions used, bounded depth | needed for struct invariants |
| **Callees** | signatures, doc comments, *existing accepted contracts* — not bodies | compositional; keeps the pack small |
| **Callers** | call sites with a few lines of context | how the result is relied on (`a[f(x)]` ⇒ `f(x) < len`) |
| **Neighbours / analogues** | sibling functions; the function a new one resembles | conventions, error-code idioms |
| **Tests** | unit tests touching the function (KUnit in the kernel) | examples to generalise (Paper A's trick) |

**Recipes** are subsets of slices, and are the experiment's input factor:

- **Baseline** — today's behaviour: the whole file, with our annotations and `main` removed.
- **Blind** — everything except the target body. It cannot transcribe the implementation.
- **Sighted** — everything, including the body.
- **Two-stage** — blind proposes postconditions; sighted refines domains.
- **Ablations** — sighted minus one slice at a time, to price each slice.
- **Control: generated description** — an LLM summary of the body, without the body. Tests the
  hypothesis that it is the body in disguise and inherits its bugs.

A formal spec, where one exists, is the answer rather than an input; it is rare in C and serves
only as an upper bound.

**Seams.** `ProposalRequest.source_text` must stay the slice the harness inlines, so the pack
needs an additive `context` field. The recipe id belongs on `Provenance`, which `store.py`
persists as columns, so it needs an additive column migration (the pattern `provider`/`model`
already used).

## Outputs: property kinds, ranked

1. **Representation invariants of struct arguments (new, top priority).** Evidence from today:
   `forseti synth` reports *clean* `ring_buffer.c::rb_push` VIOLATED — `r->buf[r->tail] = v`
   writes out of bounds (CWE-787) because nothing bounds `tail`. At kernel scale nearly every function takes
   a struct pointer, so without these even S0 is noise. Check them inductively, per function:
   assumed at entry and asserted at exit of every mutator, established by initialisers
   (`rb_init`). An over-strong invariant hides bugs, so it must also be discharged where callers
   construct the struct (generalising RFC-0003 S3).
2. **Contracts** (pre/postconditions) — exists.
3. **Two-call (metamorphic) properties** — round-trip `decode(encode(x)) == x`, idempotence,
   monotonicity, determinism. Body-independent and cheap for ESBMC; needs a renderer.
4. **Pre-state and frame conditions** — `old(...)` references: permutation, "bytes outside
   `[0, n)` unchanged". Needs the renderer to snapshot inputs before the call.
5. **Sequence properties** for module protocols, where per-function invariants are not enough.

**Deferred.** Loop invariants pay off for k-induction or an unbounded backend, not for bounded
unrolling — revisit with k-induction or the Lean stretch. Intermediate asserts are tied to the
implementation; they are lemmas, not intent.

Every candidate gains a `kind` and a `source` naming the slice that justified it, so results can
be attributed to inputs.

## Metrics

Per property, aggregated per cell (recipe × kind × model), all under ground rule 3:

- **M1 — held on the fixed version.** A violation on correct code is a false alarm (or a real
  bug: triage and record).
- **M2 — bug-pair discrimination (primary).** VIOLATED on the buggy version, held on the fixed
  one. Per edit event: did at least one property discriminate?
- **M3 — mutation-kill rate (secondary).** Needs #4; equivalent mutants filtered by S1, with the
  skipped count reported. Plotting M3 against M2 per recipe diagnoses transcription (high M3,
  low M2).
- **M4 — domain absorption.** The property holds on the buggy version only because the *known*
  triggering input (Tier 0 counterexample, fuzzer reproducer) lies outside its domain.
  Legitimate undefined-behaviour exclusions are not absorption.
- **M5 — cost.** LLM tokens and latency, ESBMC time, UNKNOWN rate.
- **M6 — redundancy.** Properties whose mutant kill set is a subset of another's.

Each cell uses at least 3 samples (proposals are nondeterministic). Comparisons are paired by edit
event; bootstrap confidence intervals only once N supports them.

## Corpus

**Tier 0 — `examples/` (tooling smoke test only).** Context with `main` and our headers removed;
`abs` excluded. A bug pair tests S2 only when the safety gate (S0) cannot already see the bug:

| S0 result | Meaning | Use for S2 |
|---|---|---|
| passes both versions | bug invisible to S0 | **usable** |
| passes clean, fails twin | S0 already catches it | exclude |
| fails clean | S0 is noise for this unit (e.g. missing representation invariant) | exclude until fixed |
| UNKNOWN on either | not tractable at this bound | exclude until tractable |

Observed today with the installed `forseti` 1.9.0 and ESBMC 8.3.0:

| Pair | S0 on clean | S0 on twin | Usable for S2? |
|---|---|---|---|
| `abs` | — | VIOLATED (signed overflow) | no (excluded anyway) |
| `ring_buffer::rb_push` | VIOLATED (unbounded `tail`) | VIOLATED | no — until kind 1 exists |
| `merge_sort::msort` | UNKNOWN (timeout up to k=256, `--max-len 4`) | UNKNOWN (same) | no — until tractable |
| `utf8_decode` | not needed | VIOLATED (memory) | no — S0 catches it |
| `murmurhash::murmur3_32` | not needed | VIOLATED (memory) | no — S0 catches it |
| `sha1::sha1_update` | not needed | VIOLATED (memory) | no — S0 catches it |
| `frame_checksum::sum_bytes` | not needed | VIOLATED at the `payload_checksum` call site (`discharge`) | no — S0 catches it |

**So Tier 0 has no usable bug pair today, and E0 cannot measure M2.** M2 starts at E1 —
representation invariants could make `ring_buffer` pass S0 on both versions, and pre-state with a
smaller bound could make `merge_sort` tractable — or at Tier 1. A useful prediction: with
contracts alone, `merge_sort_bug` would not be discriminated even when tractable, because
sortedness misses a dropped element and the permutation check needs pre-state (kind 4).

**Tier 1 — history replay on a mid-size C project** with `compile_commands.json` and fix commits
that identify the change they fix. For each fix: the parent of the fix is the buggy version, the
fix is the fixed version, and the introducing commit supplies the intent and the write-time
snapshot. Report the yield funnel first: fix commits → single-function fixes → harnessable →
buggy version passes S0. Expect a low yield (harnessability limits, and most fuzzer-found bugs are
memory bugs S0 already catches).

**Tier 2 — the Linux kernel.** Start with self-contained library-style code, using `Fixes:` tags
for the bug pairs and KUnit tests as the test slice. Gated on RFC-0008 (below).

A held-out split of edit events is reserved for GEPA (#5) from the start.

## Tooling

- **T1 — project index + context-pack builder.** Callers, callees, types, tests and doc comments
  at a given commit; ranked and token-budgeted. Shares its index with RFC-0008.
- **T2 — recipe registry**, `ProposalRequest.context`, and the `Provenance` recipe column.
- **T3 — per-kind renderers**: representation invariants, two-call, pre-state.
- **T4 — replay runner**: checks out the parent snapshot (a git worktree), builds the pack, runs
  the proposer, checks properties on the buggy and fixed versions, and writes one JSONL row per
  property × target into the existing event trace.
- **T5 — report and viewer**: the recipe × kind table, the M3-vs-M2 scatter, and a side-by-side
  view of code, properties (tagged by source slice) and verdicts. It doubles as the demo for the
  Igalia talk.

All of it lives under `src/forseti/`, so the CI lint, type and test gates cover it.

## Dependencies and risks

- **RFC-0008 (to write): project-scale unit slicing.** Building a compilable slice of a function
  inside a large project — compile flags from `compile_commands.json`, macros, callees replaced by
  stubs or contracts — is shared by the harness and the context pack. ESBMC's handling of kernel
  code (GCC extensions, inline asm, callees without a body) is an *unverified* risk, not a claim.
- **Latency.** A proposer call takes minutes (the client's timeout is 240 s); far too slow per edit
  on the kernel. Proposals stay out of band, as `property_gate` already treats them, and only
  accepted properties block.
- **Cost.** Recipes × kinds × samples × events multiplies quickly; E0 is sized below.

## Phasing

- **E0 — smoke test on Tier 0 (contracts only).** Baseline, blind, sighted × 1 model × 3 samples
  × 6 kernels ≈ 54 proposer calls and ~650 ESBMC runs (~6 properties × 2 versions each). With a
  30 s per-run timeout the worst case is ~5.5 hours of ESBMC; one `msort` run already hit a 120 s
  timeout, so `msort` needs a smaller `--max-len` or is dropped. Measures the machinery, M1 and
  M5 only — not M2 (Tier 0 has no usable pair). Needs a minimal T1, T2 and T4 — not #4's scorer,
  not new renderers — so it runs alongside RFC-0006's S1 → scorer → GEPA order.
- **E1 — new kinds on Tier 0; first M2.** Representation invariants first (could make
  `ring_buffer` usable), then pre-state (tests the `merge_sort` prediction), then two-call.
- **E2 — Tier 1 replay.** The yield funnel, then the recipe × kind grid; M3 joins once #4 lands.
- **E3 — kernel pilot**, after RFC-0008.
- **E4 — the winning recipes become GEPA's proposer portfolio**, evaluated on the held-out split.
  This is a second GEPA pass: per ADR-0006, GEPA v1 (P3, #5) runs first on Tier 0 plus mutants,
  and E2–E4 are P4's real-code work.

The Igalia talk fits after E1 or E2: the inputs × outputs grid on both metrics, blind vs. sighted,
good and bad example properties, and the caveats. No date is assumed here.

## Open questions

1. **Project index:** clang-based (via `compile_commands.json`), or `ctags`/`git grep` as a
   fallback? How far beyond the translation unit do callers and callees reach?
2. **Unit identity** when a function is renamed or moved: what happens to `path::symbol` and its
   carried-forward properties?
3. **Intent text in experiments:** in replay it must predate the fix. Who writes it for Tier 0
   without seeing the bug?
4. **Granularity:** when is the unit a function, and when a module API (sequence properties)?
5. **Model as a factor:** fix one model for E0–E2, and compare models later?
6. **Representation invariants:** proposed per struct type (shared by every function taking it)
   or per function?

## References

- RFC-0006 (positioning; S1 equivalence), RFC-0003 (memory preconditions and discharge),
  RFC-0002 (natural bugs); ADR-0009 (property pipeline).
- #4 (W3 grading), #5 (W4 GEPA), #182 (when a property becomes a fix obligation).
- Ye et al., arXiv:2610.30188; Anthropic, *Formal verification* (Oct 2026).
