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
5. Remove the issue's `agent-ready` label after the PR is open.
6. If the work cannot proceed, leave a `gh issue comment` describing what blocked it and exit cleanly.

## Constraints

- **You are running unattended.** No operator will respond to prompts, approve tool calls, or read intermediate output during this run.
- **Use the local `gh` CLI for every GitHub mutation** (`gh issue ...`, `gh pr ...`, `gh issue comment ...`, `gh issue edit ...`). Do not call GitHub MCP connector tools (for example `add_issue_labels`, `create_pull_request`); they elicit operator approval through the provider transport and end the run as `input_required`.
- **Do not self-apply `needs-human` or any other handoff label as an exit strategy.** Use the comment-and-exit path in step 6; the operator owns label triage.
