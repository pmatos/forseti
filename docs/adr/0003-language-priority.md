# ADR-0003 — Language priority: C → C++ → Python

- **Status:** Accepted
- **Date:** 2026-06-16

## Context

ESBMC speaks several languages (C, C++, Python, Solidity, CUDA, Java/Kotlin). The loop,
property generation, grading, and counterexample parsing all have per-language cost. We can't
do all frontends well at once and need a priority order.

## Decision

Target languages in this order: **C first, then C++, then Python.** Other ESBMC frontends are
out of scope for H2.

## Consequences

- C is the most mature ESBMC frontend → lowest engine risk for P0–P3, where we want the
  research variables (properties, grading, GEPA) clean rather than fighting the frontend.
- C++ enters in P4 with the real-code push (and is where known engine gaps live — incomplete
  `std::string` model, alignment-attribute segfaults — feeding the W5 fork-and-fix stream).
- Python is a reach for P5 if the loop generalizes; counterexample parsing is frontend-aware
  precisely so adding Python later is incremental.
