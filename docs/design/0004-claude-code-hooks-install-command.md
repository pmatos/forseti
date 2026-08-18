# Design RFC 0004 — `forseti enable-project`: installing the Claude Code verify-gate hooks

- **Status:** Draft / RFC (thinking aid — not yet an ADR)
- **Date:** 2026-08-18

## Problem

The Claude Code verify-gate (`adapters/claude-code/`) today is enabled by hand: copy a JSON
block from the README into the target project's `.claude/settings.json`, substituting an
absolute path (`ABS_PATH`) to wherever `adapters/claude-code/` happens to live on disk. That
absolute path is the same class of bug this session already hit and fixed for the `forseti`
CLI itself: `~/.local/bin/forseti` was a stale `uv tool install --editable` pointing at a
worktree (`session-804f23ed`) that had since been deleted, so the CLI silently stopped
resolving. A hand-copied `ABS_PATH` in a project's `settings.json` has the identical failure
mode — point it at a worktree, delete the worktree, the gate breaks with no signal until
someone tries to use it.

We want a `forseti enable-project` command that installs (or updates) the hooks
programmatically, without reintroducing that fragility.

## Decision

### Ship the hook logic inside the `forseti` package

`adapters/claude-code/hooks/*.py` moves into `src/forseti/adapters/claude_code/` as a real
importable subpackage (`forseti_gate.py`, `event_log.py`, `post_tool_use.py`, `post_bash.py`,
`session_start.py`, `stop_gate.py` — names unchanged, only location). `[tool.setuptools.packages.find]`
already discovers everything under `src/`, so this ships automatically with `pip install`/`uv tool
install` — no `package_data`/`MANIFEST.in` needed. Its tests move from `adapters/claude-code/tests/`
to `tests/adapters/claude_code/`, which as a side effect closes a known gap: the canonical
`ruff`/`mypy` invocation (`src tests`) and `pytest`'s `testpaths = ["tests"]` never covered
`adapters/` before, so a broken adapter could hide behind a green CI run.

`adapters/claude-code/` at the repo root keeps only what stays human-facing: `README.md`,
`demo/abs64.c`, and the plugin manifest (`.claude-plugin/plugin.json`, `hooks/hooks.json`). The
plugin route and `enable-project` route end up sharing one implementation (below), so there is
one place hook behavior can drift, not two.

### Invoke hooks via a `forseti` subcommand, not a script path

A new subcommand, `forseti claude-code-hook <session-start|post-tool-use|post-bash|stop-gate>`,
follows the existing `_add_<name>_parser`/`_run_<name>` dispatch pattern in `core/cli.py`
(`verify`, `list-units`, `synth`, `discharge`, `propose`, `mcp`). It reads the Claude Code hook
JSON payload from stdin and calls straight into the moved hook logic, preserving the exact
exit-code/stdout contract Claude Code's hook protocol expects today.

This is what `enable-project` writes into `settings.json`'s `"command"` field —
`"forseti claude-code-hook post-tool-use"`, not a `python3 <path>` invocation. The alternative
(`python3 -m forseti.adapters.claude_code.post_tool_use`) was considered and rejected: the
`python3` resolved from `PATH` at hook-execution time is not guaranteed to be the interpreter
`forseti` was installed into (a `uv tool install` isolates into its own venv), so that form
would need to pin an absolute interpreter path — the exact kind of disk-path fragility this
design is trying to avoid. Dispatching through the `forseti` executable itself needs nothing new:
the hooks already require `forseti` on `PATH` to shell out to `forseti verify`, so this reuses
an existing invariant instead of adding one.

`adapters/claude-code/hooks/hooks.json` (the plugin manifest) is rewritten to use the same
`forseti claude-code-hook <name>` commands instead of
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/post_tool_use.py"`.

### `forseti enable-project [PROJECT_DIR] [--shared]`

- `PROJECT_DIR` — optional positional, defaults to `.` (the project root containing, or to
  contain, `.claude/`).
- `--shared` — write to `.claude/settings.json` (git-committed, applies to everyone who opens
  the project) instead of the default `.claude/settings.local.json` (gitignored, personal —
  the right default for "let me try this out").
- No `--function`-style knobs beyond that; the four hook entries (`SessionStart`,
  `PostToolUse` × 2 matchers, `Stop`) and their timeouts are fixed, mirroring
  `adapters/claude-code/hooks/hooks.json` today (60s / 300s / 300s / 120s).

**Update semantics: always regenerate, no version tracking.** Every run rebuilds forseti's
hook block from whatever `forseti` version is currently installed and writes it — idempotent
by construction. There is no marker file and no version comparison: upgrading `forseti`
(`uv tool upgrade forseti`) and rerunning `enable-project` always converges the target
project to current. A version marker was considered and rejected as unneeded complexity — the
only thing it would buy is skipping a cheap, already-idempotent rewrite.

**Merge algorithm** (idempotent, non-destructive): for each hook event
(`SessionStart`/`PostToolUse`/`Stop`) already present in the target settings file, strip any
existing hook entries whose `"command"` starts with the literal `"forseti claude-code-hook "`
(forseti's ownership marker), drop any matcher-group left with an empty `hooks` array by that
removal, then append freshly generated forseti entries for that event. Anything not carrying
the marker — other tools' hooks, other matchers, unrelated top-level keys like `permissions` or
`env` — is left untouched. A missing settings file is created. Malformed existing JSON raises a
typed error and aborts rather than silently overwriting (consistent with this repo's existing
error-handling convention: programmer/caller-facing failures raise, they don't get swallowed).
The write itself is atomic (temp file + rename) so a crash mid-write cannot leave a half-written
`settings.json` behind.

## Testing

- Merge-function unit tests: fresh file; existing file with unrelated hooks/keys preserved
  byte-for-byte; existing file with stale forseti entries replaced without duplication;
  malformed JSON raises and leaves the file untouched.
- `claude-code-hook` dispatcher unit tests: each `<name>` routes to its handler; an unknown
  name errors instead of silently no-op'ing.
- Existing hook-behavior tests (`adapters/claude-code/tests/` → `tests/adapters/claude_code/`)
  move as-is; their assertions don't change, only their location and the module paths they
  import.

## Out of scope

- The `codex`/`opencode` adapters are untouched — this RFC only relocates and wires the
  Claude Code adapter's hooks.
- No `forseti disable-project` / hook-removal command. Not asked for; add if it comes up.
- No auto-detection of which harness a project uses. `enable-project` installs the Claude Code
  hooks specifically; a multi-harness `--harness` flag is future scope if a second harness needs
  the same treatment.
