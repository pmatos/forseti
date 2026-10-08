# Forseti planning stage: issue #{{issue.number}} {{issue.title}}

You are the **planning** agent, running unattended in the existing issue workspace. Do not write
production code or tests in this stage. Produce a written plan that the implementation stage will
execute.

## Source of truth

- Project overview, canonical commands, vocabulary and conventions: `CLAUDE.md`
- The 6-phase plan, exit criteria, and risk register: `docs/roadmap.md`
- Accepted decisions and their rationale: `docs/adr/` (index in `docs/adr/README.md`)
- Active architecture discussion (neutral Core + per-harness adapters): `docs/design/`, starting
  with `docs/design/0001-harness-portability.md`
- Per-harness adapters and the prompt+tools fallback contract: `adapters/`
- Worked loop turns: `docs/walkthroughs/`
- Live task tracking is on GitHub (`gh issue list`), not in-repo: milestones are phases, epic
  issues are drill-downs (ADR-0005)

Project-specific constraints the plan must respect:

- **Vocabulary discipline.** ESBMC emits no proof object, only a verdict: `VERIFIED` (no violation
  up to bound k), `VIOLATED` (with a counterexample), or `UNKNOWN`. Never write "proof" where
  "reproducible verdict" is meant, and never over-claim soundness. Treat `UNKNOWN` as a distinct
  loop state; never plan to pass it silently.
- **ADRs are immutable once Accepted.** To change a decision, plan a new superseding ADR named
  `docs/adr/YYYY-MM-DD-HHMM-slug.md` (UTC); never edit an accepted one. `0001`-`0009` are frozen
  legacy numbers, and `tests/docs/test_adr_citations.py` checks that `ADR-NNNN` citations resolve.
- **Scope guardrails.** Vow is out of scope (ADR-0008). Lean is a stretch, off the critical path
  (ADR-0007). Target languages in order C, then C++, then Python (ADR-0003).
- **ESBMC invocation.** Prefer `--unwind N --no-unwinding-assertions` over `--incremental-bmc`.
- **Store.** The per-project store `.forseti/forseti.db` is gitignored; verification units are
  keyed at function level as `path::symbol`.
- **Dependencies.** The MCP SDK is an optional extra; the base install and the loop/esbmc path stay
  dependency-free.
- **Tests.** ESBMC-gated integration and corpus tests skip when `esbmc` is not on `PATH`; plan unit
  tests with fakes/ports so the slice is verifiable without ESBMC, and flag any slice that needs it.

## Issue under work

- Number: #{{issue.number}}
- Title: {{issue.title}}
- URL: {{issue.url}}
- Labels: {{issue.labels}}

### Issue body

{{issue.body}}

## Run context

- Project: {{project.name}}
- Run id: {{run.id}}
- Attempt: {{run.attempt}}
- Workspace: {{workspace.path}} (branch {{branch.name}})

## What to do

1. **Invoke the `pm-plan` skill** (via the Skill tool) with the issue number, title, and body as its
   task. Let it run its full workflow: reconnaissance, complexity classification, codebase
   exploration, drafting, validation, and adversarial review. It writes the plan to
   `.ultraplan/<plan-name>.md`.
2. The skill's "read-only mode" applies to the skill's own steps. Once it has finished, copy its
   plan file to `{{workspace.path}}/PLAN.md` (`cp .ultraplan/<plan-name>.md PLAN.md`) and commit
   `PLAN.md` as described under Exit. That copy and commit are this stage's deliverable.
3. Make sure the plan's steps are ordered TDD slices: each names the test file/location, the
   behavior under test, and the production code that makes it pass, preferring vertical slices over
   horizontal refactors. Add an **Out of scope** section naming what this PR deliberately does not
   bundle. If the skill's output lacks either, add them to `PLAN.md` before committing.

## Overrides for unattended mode

The skill is written for an interactive session. In this run:

- **Never ask the user anything.** No operator will answer. Where the skill says to ask clarifying
  questions, decide the most defensible option, state the assumption in the plan's Risks section,
  and proceed.
- **Skip the skill's Step 7** ("Ready to execute this plan, or do you want changes?"). Do not
  present the plan and wait; commit it and finish.
- **Many small changes beat one large change.** If the issue is broad, plan the minimal first slice
  that closes the issue and list the rest as follow-ups. Do not bundle refactors into a bug fix.
- Plan updates to `docs/adr/` (a new ADR), `docs/roadmap.md`, or `docs/design/` whenever the work
  resolves an architecture decision.
- Plan for the quality gate named in `CLAUDE.md`: `ruff check src tests`,
  `ruff format --check src tests`, `ty check src tests`, and `pytest -q`.
- The orchestrator squash-merges the PR, taking the subject from the PR title. Do not plan for
  merge commits, rebase merges, or a human merging.

## Constraints

- Do not write production code or tests in this stage. Only `PLAN.md`.
- Use the local `gh` CLI for every GitHub mutation. Do **not** call the GitHub MCP connector tools:
  they elicit operator approval and end the run with `terminal_reason="provider requested input"`.
- Do not modify operational labels in the `sym:*` namespace and do not self-apply `needs-human`.
- Do not run `sudo`.
- If you delegate research to sub-agents, their reports are input to the plan, not the deliverable.
  You must still write `PLAN.md` and commit it; ending your turn with only a sub-agent's report is
  a failed run.

## Exit

**You must commit `PLAN.md` before exiting.** The workflow advances to implementation only if this
run leaves a new commit on the branch, so an uncommitted plan fails the run.

```sh
git add PLAN.md
git commit --no-verify -m "docs(plan): add implementation plan for issue #{{issue.number}}"
```

`--no-verify` is deliberate: this commit is a stage-handoff artefact (the implementation stage
`git rm`s `PLAN.md` before opening the PR), so it never reaches `main` and there is nothing for
`commitlint` to protect. Running hooks here has wedged planning runs under load. Use the message
above verbatim. Commit `PLAN.md` only; do not add `.ultraplan/`. Do not push and do not open a PR.

Then end with a `success` claim.

If you cannot produce a coherent plan (the issue is contradictory, or already resolved), post
`gh issue comment {{issue.number}} --body "<what blocks planning>"`, do not commit, and end with a
`blocked` claim carrying the same explanation. A Bash tool call's `exit 1` only ends that subshell,
not the provider session, so the final claim is what routes the run to its blocked exit.
