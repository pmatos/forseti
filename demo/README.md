# Forseti demo

Shows the real `write -> verify -> counterexample -> fix` loop live: a
`claude` session on the left, an observability view of what Forseti and
ESBMC are doing underneath on the right. Nothing here is staged — see
`fixtures/README.md` for what a real recorded run looked like, including an
honest `UNKNOWN` rather than a forced pass.

## Quick start

```sh
demo/run_demo.sh                 # fresh scratch workspace, random tmux session name
demo/run_demo.sh /tmp/my-demo    # reuse a specific workspace
demo/run_demo.sh /tmp/my-demo --port 8080
```

Opens a browser tab with the live diagrammatic canvas, and (if `tmux` is
available) a split session: `claude` on the left, `demo/render.py`'s
textual view on the right, as a no-GUI/SSH-friendly companion to the canvas.
Ctrl-C or detach (`Ctrl-b d`) to leave; the canvas server shuts down when the
script exits (reattach with the `tmux attach -t <name>` it prints, restart
`demo/canvas/server.py` separately for the canvas if you come back to it).

Requires this checkout's own `forseti` build, not a separately installed
release — `run_demo.sh` sources `env.sh` itself, so it always gets whatever
is on the current branch, fixes included, even before those fixes ship in a
release.

## Pieces

| Path | What it is |
|---|---|
| `env.sh` | Puts `demo/bin` (the CLI event-logging shim) and this checkout's `.venv/bin` ahead of everything else on `PATH`. Source it before any manual step below. |
| `scaffold/` | `CLAUDE.md` (the workflow contract copied into every demo workspace) + `init.sh` (materializes one). |
| `bin/forseti` | A PATH shim logging every `forseti` CLI call as a `.forseti/events.jsonl` event (`synth`/`discharge`/`semantic-loop`/etc aren't self-instrumented today). |
| `render.py` | Stdlib-only terminal renderer: live-tails or replays `.forseti/events.jsonl` as a colored one-line-per-event sequence. |
| `canvas/` | Browser-based diagrammatic view of the same event stream (4-actor node diagram + full scrollable log), live or replay. |
| `fixtures/` | A real recorded run + a standalone e2e assertion script pinning its shape. |
| `run_demo.sh` | Ties the above together: scaffold, start the canvas, open a browser, launch `claude` (+ `render.py`) in tmux. |

Each piece's own README has the details (`scaffold/README.md`,
`bin/README.md`, `render_README.md`, `canvas/README.md`, `fixtures/README.md`).

## Manual (no launcher) walkthrough

```sh
source demo/env.sh
demo/scaffold/init.sh /tmp/my-demo
python3 demo/canvas/server.py /tmp/my-demo &     # or demo/render.py for the terminal view
cd /tmp/my-demo && claude
```

## Why the loop sometimes ends in UNKNOWN, honestly

`forseti synth` (deterministic memory-safety) reliably settles. `forseti
semantic-loop --mode propose` (the LLM-invariant path) currently cannot
bound a `(ptr, len)` buffer's length the way `synth` does with `--max-len`
(issue #299), so a proposed property over such a buffer often reports
`UNKNOWN` rather than `held`/`violated` — not a defect in the code under
test, a real tooling ceiling. The demo doesn't work around this: an honest
`UNKNOWN`, correctly identified as such and never treated as a pass, is
itself part of what the loop is supposed to show.
