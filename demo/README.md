# Forseti demo

Shows the real `write -> verify -> counterexample -> fix` loop live: a
`claude` session on the left, an observability view of what Forseti and
ESBMC are doing underneath on the right. Nothing here is staged — see
`fixtures/README.md` for what a real recorded run looked like, including an
honest `UNKNOWN` rather than a forced pass.

## Prerequisites

On a machine that has never run Forseti:

| Need | Why | How |
|---|---|---|
| Python >= 3.12 | Forseti itself (the demo scripts are stdlib-only) | your package manager, or `uv` |
| A clone of this repo | the demo runs from a checkout, not a release build | `git clone https://github.com/pmatos/forseti && cd forseti` |
| An editable install | `demo/env.sh` puts this checkout's `.venv/bin` ahead of any released `forseti` on `PATH` | `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"` (or `uv venv && uv pip install -e ".[dev]"`) |
| ESBMC 8.5.0 on `PATH` | every verdict comes from it | see below |
| The `claude` CLI, logged in | the left-hand session, and `forseti propose` shells out to `claude -p` for candidate properties | install and log in per Anthropic's Claude Code documentation |
| `git` | `scaffold/init.sh` makes each workspace its own repo | your package manager |
| `tmux`, a browser | optional: without tmux `run_demo.sh` runs `claude` directly; without `xdg-open`/`open` it prints the canvas URL | your package manager |

**ESBMC.** CI pins the upstream v8.5 release (ESBMC 8.5.0):

```sh
curl -fLO https://github.com/esbmc/esbmc/releases/download/v8.5/esbmc-linux.zip
echo "d8da304dd0dfce6c9f488379f03a72e768bce8598c22478c980e1704247352f1  esbmc-linux.zip" | sha256sum --check --strict -
unzip -q esbmc-linux.zip -d ~/esbmc
export PATH="$HOME/esbmc/release/bin:$PATH"     # add to your shell profile
esbmc --version                                  # ESBMC version 8.5.0
```

The digest is copied from `.github/workflows/ci.yml` (`ESBMC_SHA256`); if that
file has moved on, trust it over this page. Other platforms: build or download
a matching 8.5 release from <https://github.com/esbmc/esbmc/releases>.

Sanity check before a live run (no Claude or ESBMC needed for the first line):

```sh
python3 demo/fixtures/assert_recorded_run.py     # PASS: ... (32 events) ...
source demo/env.sh && forseti --version && esbmc --version
```

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
| `bin/forseti` | A PATH shim logging every `forseti` CLI call as a `.forseti/events.jsonl` event (`synth`/`discharge`/`semantic-loop` now emit their own `cli.command` event, #301, but the demo renderers don't consume it yet). |
| `render.py` | Stdlib-only terminal renderer: live-tails or replays `.forseti/events.jsonl` as a colored one-line-per-event sequence. |
| `canvas/` | Browser-based diagrammatic view of the same event stream (4-actor node diagram + full scrollable log), live or replay. |
| `fixtures/` | A real recorded run + a standalone e2e assertion script pinning its shape. |
| `run_demo.sh` | Ties the above together: scaffold, start the canvas, open a browser, launch `claude` (+ `render.py`) in tmux. |

Each piece's own README has the details (`scaffold/README.md`,
`bin/README.md`, `render_README.md`, `canvas/README.md`, `fixtures/README.md`).

## Running it somewhere else

- **On a remote machine over SSH.** The canvas server binds `127.0.0.1` only,
  so tunnel it and open the tunnel locally. `run_demo.sh` can't open a
  browser over SSH, so it prints the URL:

  ```sh
  ssh -L 8765:localhost:8765 user@host      # then, on the host:
  demo/run_demo.sh                          # tmux: claude + render.py
  # on your laptop: open http://localhost:8765
  ```

  Use `--port N` on both sides if 8765 is taken. With no browser at all, the
  `render.py` pane on the right is the whole observability view.
- **Without Claude, ESBMC, or network (a talk, a flaky venue).** Replay the
  recorded real run; it needs only Python:

  ```sh
  python3 demo/canvas/server.py --replay demo/fixtures/recorded_run.jsonl --speed 4
  python3 demo/render.py --replay demo/fixtures/recorded_run.jsonl --speed 4
  ```

  `--speed 1` is real time (the run took ~4 minutes, mostly the LLM proposer);
  omit `--speed` for instant. Say it's a recording — it is.
- **A live run takes a few minutes.** `forseti synth` settles in under a
  second, but `semantic-loop --mode propose` waits on an LLM call (up to its
  240 s default timeout), and the honest outcome is often `UNKNOWN` (see
  below) — plan for that or fall back to the replay.
- **Scratch workspaces are throwaway.** `run_demo.sh` with no argument makes a
  fresh `mktemp -d` workspace each time; nothing writes into your checkout.

## Manual (no launcher) walkthrough

```sh
source demo/env.sh
demo/scaffold/init.sh /tmp/my-demo
python3 demo/canvas/server.py /tmp/my-demo &     # or demo/render.py for the terminal view
cd /tmp/my-demo && claude
```

## Why the loop sometimes ends in UNKNOWN, honestly

`forseti synth` (deterministic memory-safety) reliably settles. `forseti
semantic-loop --mode propose` (the LLM-invariant path) also bounds a
`(ptr, len)` buffer's length to `--max-len` (default 8, as `synth` does; issue
#299) unless the proposed property's own `domain` already names the length, and
each verdict says which bound it was checked under (`len<=8`). Before that fix
such a property could only ever report `UNKNOWN`, which is what the recorded run
below shows. An `UNKNOWN` that remains is still reported honestly rather than
treated as a pass — a `-k` at or below the length bound, or a `domain` that
gives a lower bound on the length but no upper one, are the usual causes.
