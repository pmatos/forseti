# demo/bin -- forseti CLI PATH shim

`forseti` in this directory is a **demo-only** wrapper around the real
`forseti` binary. It exists so the demo's live "observability" pane can see
`forseti synth` / `forseti discharge` (and any other subcommand) invocations,
which the Forseti package does not instrument today -- unlike the Claude Code
adapter's hooks and Forseti Core's `propose`/`check` calls, which already log
to `.forseti/events.jsonl` on their own (see
`src/forseti/adapters/claude_code/event_log.py` and `src/forseti/core/events.py`).

This is **not a product feature**. It does not modify, wrap, or replace
anything inside `src/forseti/`; it is a shell-out wrapper that lives entirely
under `demo/`.

## Usage

Put this directory *ahead of* the real `forseti`'s directory on `PATH` before
launching the demo session -- `source demo/env.sh` (see `../env.sh`) does
this and also puts this checkout's own `.venv/bin` ahead of any separately
installed `forseti` release, e.g.:

```sh
source demo/env.sh
claude
```

Every `forseti <args...>` invocation Claude makes will then go through the
shim, which:

1. Finds the real `forseti` binary by searching `PATH` itself, skipping
   `demo/bin` (so it doesn't just call itself).
2. Records the start time and argv.
3. Runs the real `forseti` with the same argv, stdin, stdout, and stderr --
   output still streams to the terminal live, exactly as if the shim weren't
   there. Nothing is swallowed.
4. Once it exits, appends **one** JSON line to `<cwd>/.forseti/events.jsonl`
   (created if missing) with:
   - `ts` -- epoch seconds (matches the real event log's convention)
   - `type` -- always `"cli"`
   - `argv` -- the full argument list, e.g. `["forseti", "synth", "unit.c", ...]`
   - `subcommand` -- `argv[1]` if present, else `null`
   - `exit_code`
   - `duration_s`
   - `output_tail` -- the last ~4000 characters of combined stdout+stderr

## Limitations

- **Truncated output.** Only the last ~4000 characters of combined
  stdout+stderr are kept in `output_tail`, to avoid bloating the trace with
  large ESBMC output. The terminal/Claude still see the full output live --
  only what's written to the trace file is bounded.
- **No double-logging of hook calls.** `forseti claude-code-hook ...`
  invocations (`post-tool-use`, `session-start`, `stop-gate`, `post-bash`) are
  already fully captured by the adapter's own `edit`/`verify`/`gate`/`stop`
  events in the same file, so the shim detects `argv[1] == "claude-code-hook"`
  and runs the real command without appending a `cli` event for it.
- **cwd-keyed, not project-root-keyed.** The event is appended to
  `<cwd>/.forseti/events.jsonl` (the directory `forseti` was invoked from), not
  the nearest ancestor containing a `.forseti/` directory. Fine for the demo
  workspace (a single flat project dir); a real `.git`-style upward search
  isn't implemented.
- **Best-effort, never fails the command.** If the trace file can't be written
  (permissions, missing parent, disk full), the shim silently drops the event
  and still returns the real command's exit code -- matching
  `event_log.py`'s own swallow-`OSError` convention.
- **This is scaffolding for the demo, not the product's event schema.** A
  downstream renderer treating `.forseti/events.jsonl` as the single source of
  truth for the live pane will see `cli` events interleaved with the real
  `edit`/`verify`/`gate`/`stop`/`property.*`/`gate.decision` events, but only
  the latter are covered by any compatibility guarantee.
