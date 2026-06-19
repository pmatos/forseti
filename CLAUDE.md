# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Forseti is an H2-2026 research program that puts the **ESBMC** bounded model checker *inside*
the agent coding loop: `write → verify → counterexample → fix`. The agent only hands over code
that already passed.

It is currently at **planning / foundation stage**: `src/` holds only placeholder READMEs, and
there is **no build system, package manifest, or test suite yet** (implementation lands P1+).
Don't go hunting for one.

- The 6-phase plan, exit criteria, and risk register: `docs/roadmap.md`
- Decisions and their rationale: `docs/adr/` (index in `docs/adr/README.md`)
- Active architecture discussion (the neutral Core + per-harness adapters direction):
  `docs/design/0001-harness-portability.md`
- Live task tracking is on GitHub, not in-repo: `gh issue list` — Milestones = phases,
  epic issues → sub-issues = drill-down (ADR-0005).

## Vocabulary discipline (important)

ESBMC emits **no proof object.** For a unit + property it returns a **verdict**:
`VERIFIED` (no violation found *up to bound k*), `VIOLATED` (+ a concrete counterexample), or
`UNKNOWN` (timeout / k too small). **Never write "proof" in this repo where we mean
"reproducible verdict."** A genuine, kernel-checkable proof is the Lean branch's job, not
ESBMC's. Don't over-claim soundness: bounded = "proven up to k," not for all inputs.

## Conventions

- **ADRs are immutable once Accepted.** To change a decision, add a new superseding ADR —
  never edit an accepted one. Numbering is `docs/adr/NNNN-title.md`.
- **Scope guardrails:** Vow is **out of scope** (ADR-0008). Lean is a stretch, **off the
  critical path** (ADR-0007). Target languages in order **C → C++ → Python** (ADR-0003); other
  ESBMC frontends are out for H2.
- The per-project store is `.forseti/forseti.db` (SQLite), **gitignored** — machine-generated,
  not source. Verification units are keyed at function level as `path::symbol`.

## ESBMC

- `esbmc` 8.3.0 is installed at `~/.local/bin/esbmc`. The project pins to our **own fork** and
  upstreams fixes in curated batches (ADR-0004).
- Prefer `--unwind N --no-unwinding-assertions` over `--incremental-bmc` (the latter is known to
  yield spurious `UNKNOWN` on small unwinds). Treat `UNKNOWN` as a distinct loop state — raise k,
  simplify the harness, or report; **never silently pass it.**
