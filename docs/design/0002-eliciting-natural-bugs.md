# Design RFC 0002 — Eliciting natural bugs: demonstrating the loop without staging defects

- **Status:** Draft / RFC (thinking aid — not yet an ADR)
- **Date:** 2026-06-22

## Problem

The loop's value story is *"the agent hands you the version that already passed."* The convincing
demo is the loop **catching and fixing a real bug**. But a capable writer model (e.g. Claude Code on
a memorized trap like `abs`/`INT64_MIN`) writes correct code on the first try — so `verify` returns
VERIFIED, no counterexample, no fix iteration, and there is nothing visible to show.

The naive fix — *prompt the agent to write a bug* — makes the demo **staged and circular**: it no
longer shows the loop catching mistakes the agent would actually make. So: how do we exercise
`write → verify → cex → fix` end to end **without instructing the writer to fail**?

## The distinction that dissolves most of it

Two goals are easily conflated; the difficulty only bites the second.

| Goal | What it shows | Is seeding a known bug OK? |
|---|---|---|
| **Mechanism demo** | the loop *plumbing* transitions correctly (VIOLATED → read cex → fix → VERIFIED) | **Yes** — it's a unit test of the harness, not a claim about the agent. `examples/abs.c` is exactly this. |
| **Scientific claim** | ESBMC-in-the-loop catches bugs the agent *would otherwise ship* | **No** — staging the bug is circular; this is the Jan-2027 "self-corrects on a non-toy example" bar. |

For the mechanism demo (and the P0 manual turn, walkthrough 0001), a hand-seeded defect is
legitimate and need not feel contrived. The rest of this RFC is about the scientific claim.

## Strawman: surface natural bugs, don't manufacture them

- **Adversarial task selection, not adversarial prompting.** Choose tasks at the *frontier of the
  writer's competence*, where the failure is latent — not tasks where you instruct failure. The
  kernel corpus (#13: ring-buffer wraparound, UTF-8 boundaries, MurmurHash, merge-sort bounds) is
  well-suited: real edge cases a model misses without being told to. You pick hard tasks; you never
  say "write a bug."
- **Push on the property, not the code.** Strong properties (`--overflow-check` over *all* inputs,
  reachability properties) are violated by competent-looking code. Tension comes from property
  strength, not writer incompetence.
- **Weaker writer model.** Use a smaller/older model (e.g. Haiku) as the in-loop writer: it makes
  genuine mistakes while the loop is what's on show. *"Never hands you the broken draft"* still holds,
  now tested against a writer that actually errs.
- **Mutation testing = the principled "force a bug."** Take *correct* code and mechanically mutate
  it — an unbiased, standard fault model rather than a hand-picked staged defect. This is already the
  **P2 differential mutation-kill grading harness**; it is the rigorous way to inject defects at
  scale and measure catch rate.

## Methodological hazard

If we tune prompts/tasks *because we want failures*, we are **p-hacking the demo**. The clean
methodology: fix the task distribution and property generator **independently** of wanting failures,
then report an honest metric. **Mutation-kill rate is unbiased precisely because it doesn't depend on
coaxing the writer into mistakes.** The headline demo is therefore better framed as *fish for real
failures across a corpus* (log first-drafts that were genuinely VIOLATED and then fixed; showcase a
captured one) than *engineer one failure in a single run*.

## Where this lands

- **P1 (the loop):** task selection must aim at the competence frontier, and the writer model is a
  knob (consider a deliberately weaker writer for evaluation runs).
- **P2 (grading harness):** mutation-kill is the unbiased fault injector and the reportable metric;
  it is the answer to "show the loop catches bugs without staging them."

## Open questions

- Do we evaluate with a deliberately weaker writer, the same writer we ship, or both (gap = value of
  the loop)?
- How do we log/curate genuine in-the-wild VIOLATED→fixed instances for the public writeup without
  cherry-picking?
- Does mutation-kill on a kernel correlate with catching natural writer bugs, or only synthetic ones?

Related: [[0001-harness-portability]], roadmap P1/P2 (`docs/roadmap.md`), ADR-0006 (sequencing
property-gen + GEPA before real code).
