# ADR-0005 — Hybrid tracking (in-repo + GitHub)

- **Status:** Accepted
- **Date:** 2026-06-16

## Context

The investment needs progress tracking with drill-down to individual tasks, across work that
spans this repo plus PRs landing in `esbmc/esbmc` and the ESBMC plugin. Pure GitHub Issues
buries rationale in comment threads; pure in-repo markdown lacks a live board.

## Decision

Use a **hybrid** model:

- **In-repo, versioned markdown** for durable content: `docs/roadmap.md` (the narrative spine
  + risk register) and `docs/adr/` (decisions). Greppable, agent-navigable, reviewable in PRs.
- **GitHub for live tracking:** Milestones = the 6 phases; Epic issues → sub-issues = the
  drill-down; a Project board = the kanban view. `roadmap.md` cross-links epics by issue number.

## Consequences

- The "why" survives in git history; the "what's in flight" lives where it's easy to update.
- One sync cost: roadmap phase ↔ GitHub milestone names must be kept aligned.
- Public repo (per project decision) means the roadmap and ADRs are part of the public story.
