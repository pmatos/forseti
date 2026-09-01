# Codex adapter

Makes **Codex** drive the Forseti `write → verify → counterexample → fix` loop
over the neutral Core. Codex has full lifecycle hooks (`PreToolUse`,
`PostToolUse`, `Stop`, …) as well as `AGENTS.md` and `notify`, so — unlike
opencode — it can **enforce** the loop, not just prompt for it. The enforcing
gate here is a `PostToolUse` hook that verifies edited units and blocks on a
counterexample; `AGENTS.md` and `notify` back it up.

| File | Role |
|---|---|
| [`verify_hook.py`](../../src/forseti/adapters/codex/verify_hook.py) | **`PostToolUse` gate** — verifies `apply_patch` edits, blocks on VIOLATED. Ships inside the `forseti` package (#212), dispatched via `forseti codex-hook verify` |
| [`install.py`](../../src/forseti/adapters/codex/install.py) | `forseti enable-project --harness codex`'s merge/write logic for `.codex/config.toml` (#212) |
| [`AGENTS.md`](./AGENTS.md) | Loop instructions Codex reads (embeds the #46 fallback block verbatim) |
| [`config.toml.example`](./config.toml.example) | Reference form of the hook wiring, plus `notify` + the Core MCP server (not installed by `enable-project`) |
| [`notify.py`](./notify.py) | Secondary `notify` reminder at turn end (log + desktop notification) |

## Install

The recommended path is `forseti enable-project` (#212): it packages
`verify_hook.py` inside the `forseti` CLI and wires it in for you, so there is
no absolute script path to keep in sync with wherever this checkout lives.
`AGENTS.md`/`notify`/the MCP server registration stay manual steps (below) —
`enable-project` only manages the `PostToolUse` gate itself.

1. **Install the Core** (the hook uses the SDK-free `forseti verify` CLI; the
   `mcp` extra additionally exposes `forseti mcp` for the model to call `verify`
   itself):

   ```bash
   pip install 'forseti[mcp]'
   ```

2. **Install the `PostToolUse` gate:**

   ```bash
   forseti enable-project --harness codex /path/to/your/project
   ```

   This writes (or idempotently updates) a `[[hooks.PostToolUse]]` entry into
   the project's `.codex/config.toml` whose command is the stable
   `forseti codex-hook verify` — dispatched in-process by the installed
   `forseti`, never a `python3 "/absolute/path/..."` invocation. Rerunning is
   safe: it always regenerates forseti's own block from the currently
   installed `forseti` version and leaves every other key/hook in the file
   untouched. `--harness codex` may be omitted if `enable-project` can
   auto-detect the session (Codex sets `CODEX_SESSION_ID`/`CODEX_THREAD_ID`);
   it refuses to guess if that's ambiguous or absent. `forseti disable-project
   --harness codex` removes only this block later, e.g. if it was installed
   for the wrong harness by mistake. `config.toml.example` still shows the
   equivalent hand-written form for reference.

3. **Give Codex the loop instructions.** Copy `AGENTS.md` to your project root,
   or merge its contents into an existing `AGENTS.md`.

4. **Wire `notify` + MCP** (not managed by `enable-project`). Merge the
   relevant parts of `config.toml.example` into `~/.codex/config.toml`,
   replacing the placeholder script path with an **absolute** path to
   `notify.py`. (`codex mcp add forseti -- forseti mcp` registers just the MCP
   server.) Note: `notify` is a top-level key and must sit *before* any
   `[table]`/`[[table]]` header, or TOML scopes it into that table and Codex
   ignores it. Codex 0.148 only accepts `notify` at the **user** level, not in
   a project-local `.codex/config.toml` — which is exactly why
   `enable-project` never writes one there.

5. **Trust the hook.** Codex **skips non-managed command hooks until you review
   and trust them** — so a freshly-wired hook does *not* run yet. In Codex, run
   `/hooks`, inspect the `PostToolUse` hook, and trust it (Codex also prints a
   startup warning when a hook needs review). Trust is keyed to the hook
   **definition** in `config.toml` (event/matcher/command/timeout/status), **not**
   what that command actually runs — so **changing the definition requires
   re-trust**, but the flip side is a feature here: the wired command is the
   stable `forseti codex-hook verify`, so **upgrading `forseti`** (which is what
   actually changes what runs) **does not re-prompt**, and trust survives a
   `forseti` upgrade. Treat the installed `forseti` as trusted code. For
   headless/CI use, `codex --dangerously-bypass-hook-trust` skips the gate —
   treat it as the escape hatch it is.

## Enforcement level: hook-enforced

- **`PostToolUse` (`verify_hook.py`) is the gate.** After an `apply_patch` edit
  it runs `forseti verify` on each edited source unit and, on a **VIOLATED**
  verdict, returns `{"decision": "block", ...}` so Codex feeds the counterexample
  back to the model — the harness enforces the fix, not prompt goodwill.
- **UNKNOWN / ERROR are surfaced, not blocked.** The hook fires on *any* edited
  file, not a registered unit, so an inconclusive result (no entry point, k too
  small) is reported via `systemMessage` rather than hard-blocking a routine
  edit. It is never silently passed; strict per-unit handling (with the raise-k
  ladder) arrives with the unit registry.
- **`AGENTS.md` + `notify` back it up.** The prompt covers edits the hook can't
  see (e.g. non-`apply_patch` shell edits), and `notify` leaves a turn-end
  reminder. For the fullest reference gate (`PostToolUse` + a `Stop`-gate + a
  property subagent), see the Claude Code adapter (#45).
