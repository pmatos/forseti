# ADR-0002 — Scope & success metric

- **Status:** Accepted
- **Date:** 2026-06-16

## Context

A 6-month, full-time investment can chase a working system, a research paper, upstream
ESBMC impact, or all three. Pursuing all three at once risks spreading thin and finishing
none. We need one unambiguous bar so milestones can be judged against it.

## Decision

The **primary** success metric is: **a demoable ESBMC-in-the-loop system** (write → verify →
counterexample → fix, self-correcting before a human sees the diff) that runs on a **non-toy**
example, **plus a public writeup** (blog / paper / talk).

ESBMC engine fixes, real-code retrofits, and a Lean backend are **supporting** work — they
serve the demo and the writeup; they are not themselves the bar.

## Consequences

- Every phase has an exit criterion that ladders up to "the loop demos on something real."
- "Non-toy" means beyond `abs()` — at minimum an algorithmic kernel the agent writes from a
  spec, ideally pushing into real C/C++ in P4.
- If time gets tight, cuts come from the supporting work (breadth, Lean, extra real-code
  examples), never from "the loop runs end-to-end and is written up."
