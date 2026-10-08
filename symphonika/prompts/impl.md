# Implementing issue #{{issue.number}}: {{issue.title}}

## Issue

{{issue.body}}

## Workspace

Work in {{workspace.path}} on branch {{branch.name}}.

## What to do

0. `{{workspace.path}}/PLAN.md` was written and committed by the planning stage. Execute it. If it is
   missing or stale, re-derive the plan from the issue first (`gh issue view {{issue.number}}`).
1. Read the issue and inspect the relevant code before editing.
2. Implement a small, focused change with behavior-focused tests, following the plan's TDD slices.
3. Run the local quality gate from `CLAUDE.md`, each as a separate command:
   - `ruff check src tests`
   - `ruff format --check src tests`
   - `ty check src tests`
   - `pytest -q`
4. Drop the plan, which is a stage-handoff artefact and must not ship: `git rm PLAN.md` and commit it
   as `chore: drop stage-handoff PLAN.md` after the quality gate has passed, as the last commit before
   pushing. `git diff --stat main...HEAD` must not list `PLAN.md`.
   Then commit, push {{branch.name}}, and open a non-draft pull request against `main` with the local `gh` CLI.
   Use a Conventional Commits PR title (`type: subject`, lower-case subject, e.g. `fix: handle unknown verdicts in the loop`) and no agent-name prefix; the `Lint PR title` check enforces it.
   **This is the actual finish line for this turn** — the orchestrator does not inspect local
   commits, only whether an open PR exists for this branch. A correct, fully-committed fix that
   never gets pushed and opened as a PR reads to the orchestrator as a wasted run: nothing
   reviews, tests, or merges it. This run is a single headless turn with no later resumption, so
   never background the quality gate and end your turn waiting for it to finish — run it in the
   foreground, and if you are close to running out of turn before it completes, stop waiting on
   it and push + open the PR anyway, noting what you did not verify in the PR description.
5. Remove the issue's `agent-ready` label after the PR is open.
6. If the work cannot proceed, leave a `gh issue comment` describing what blocked it, then end with a
   `blocked` claim carrying the same explanation.

## Constraints

- **You are running unattended.** No operator will respond to prompts, approve tool calls, or read intermediate output during this run.
- **Use the local `gh` CLI for every GitHub mutation** (`gh issue ...`, `gh pr ...`, `gh issue comment ...`, `gh issue edit ...`). Do not call GitHub MCP connector tools (for example `add_issue_labels`, `create_pull_request`); they elicit operator approval through the provider transport and end the run as `input_required`.
- **Do not self-apply `needs-human` or any other handoff label as an exit strategy.** Use the comment-and-`blocked`-claim path in step 6; the operator owns label triage.

## Exit

Once the pull request is open and `agent-ready` is removed, end with a `success` claim. The
orchestrator drives the PR from there. A Bash tool call's `exit 1` only ends that subshell, not the
provider session, so the final claim is what the FSM gates this state's advance on.
