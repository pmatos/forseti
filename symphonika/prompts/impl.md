# Implementing issue #{{issue.number}}: {{issue.title}}

## Issue

{{issue.body}}

## Workspace

Work in {{workspace.path}} on branch {{branch.name}}.

## What to do

1. Read the issue and inspect the relevant code before editing.
2. Implement a small, focused change with behavior-focused tests.
3. Run the local quality gate.
4. Commit, push {{branch.name}}, and open a non-draft pull request with the local `gh` CLI.
   **This is the actual finish line for this turn** — the orchestrator does not inspect local
   commits, only whether an open PR exists for this branch. A correct, fully-committed fix that
   never gets pushed and opened as a PR reads to the orchestrator as a wasted run: nothing
   reviews, tests, or merges it. This run is a single headless turn with no later resumption, so
   never background the quality gate and end your turn waiting for it to finish — run it in the
   foreground, and if you are close to running out of turn before it completes, stop waiting on
   it and push + open the PR anyway, noting what you did not verify in the PR description.
5. Remove the issue's `agent-ready` label after the PR is open.
6. If the work cannot proceed, leave a `gh issue comment` describing what blocked it and exit cleanly.

## Constraints

- **You are running unattended.** No operator will respond to prompts, approve tool calls, or read intermediate output during this run.
- **Use the local `gh` CLI for every GitHub mutation** (`gh issue ...`, `gh pr ...`, `gh issue comment ...`, `gh issue edit ...`). Do not call GitHub MCP connector tools (for example `add_issue_labels`, `create_pull_request`); they elicit operator approval through the provider transport and end the run as `input_required`.
- **Do not self-apply `needs-human` or any other handoff label as an exit strategy.** Use the comment-and-exit path in step 6; the operator owns label triage.
