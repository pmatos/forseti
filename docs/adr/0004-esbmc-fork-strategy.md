# ADR-0004 — ESBMC: fork now, upstream in batches

- **Status:** Accepted
- **Date:** 2026-06-16

## Context

Real-code targets will break ESBMC, and fixing the engine is an accepted, first-class
workstream ("real weight on improving ESBMC itself"). Two failure modes to avoid: (a) the loop
stalls waiting on upstream review latency, and (b) a private fork diverges so far it never
gives back, and the "improving ESBMC" impact story evaporates.

## Decision

Forseti pins to **our own ESBMC branch** for fast iteration. Fixes are **upstreamed to
`esbmc/esbmc` in curated PR batches**, with light coordination with Lucas Cordeiro and the
maintainers.

## Consequences

- The loop is never blocked on maintainer review — we always build on our branch.
- Upstream-contribution credit still accrues, just batched rather than per-fix.
- Requires discipline: keep the branch rebased on upstream and the diff curated so batches stay
  reviewable. Track divergence as a standing risk (roadmap Risk 7/11).
