# ADR-0008 — Vow is out of scope

- **Status:** Accepted
- **Date:** 2026-06-16

## Context

[Vow](https://vow-lang.com) is a *proof-native* language — one where contracts and proofs are
built in from the start, with ESBMC underneath. It is thematically adjacent to Forseti and easy
to drift into. The deck explicitly frames it as "the bigger bet … genuinely another talk,"
deliberately outside this investment.

## Decision

**Vow is out of scope for the Forseti H2 investment.** Forseti *retrofits* proofs onto code that
agents already write, in the languages they already use. Vow is a separate effort.

## Consequences

- Clear guardrail against scope creep (roadmap Risk 10): work that only makes sense for a
  proof-native language belongs to Vow, not here.
- Shared substrate (ESBMC, generated properties) may still inform Vow later — but Forseti does
  not take on language-design work.
