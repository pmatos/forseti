# ADR-0007 — Lean stretch off the critical path

- **Status:** Accepted
- **Date:** 2026-06-16

## Context

The deck floats a second, **unbounded** proof backend — Lean — so the same generated properties
could be discharged for *all* inputs, not just up to k. It is genuinely valuable but depends on
collaboration with Jesse and his available time. A deliverable on the critical path can't hinge
on someone else's calendar.

## Decision

Lean is a **backlog epic, off the critical path.** Concrete plan: a ~30-minute feasibility chat
with Jesse around **September** (P2 touchpoint); a **thin spike in P5 (Dec–Jan) only if he
commits.** The success metric ([ADR-0002](0002-scope-and-success-metric.md)) never depends on it.

## Consequences

- The investment succeeds or fails independent of Lean.
- The early scoping chat means that *if* Jesse is in, the P5 spike is prepared rather than a
  scramble.
- If he can't engage, Lean cleanly slips to a later half with no damage to the H2 bar.
