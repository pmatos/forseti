# ADR-0006 — Sequence GEPA before real-code

- **Status:** Accepted
- **Date:** 2026-06-16

## Context

After the loop skeleton works on C kernels (end of P1), two big bets compete for the next
slot: (a) property generation + differential grading + GEPA — the novel, paper-worthy
contribution — or (b) pushing the loop onto real code while hardening ESBMC — the "wow" of a
real bug caught. Doing both at once (interleave) is fastest in theory but produces two things
80%-done in December with weak milestone boundaries.

## Decision

De-risk **property-gen + grading + GEPA (P2–P3) first**, on controllable kernels with a clean,
reproducible eval. **Real-code + ESBMC push (P4) comes after.**

## Consequences

- The most novel contribution — and the heart of the writeup — is locked early, on a tractable
  eval where the variables aren't confounded by frontend breakage.
- The crowd-pleasing "caught a real bug" demo lands later (P4) and is framed as a stretch, not
  a dependency of the bar.
- If P2–P3 overrun, P4's real-code scope absorbs the slip — the writeup core is already safe.
