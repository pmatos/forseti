# Codex adapter

Makes **Codex** drive the Forseti `write → verify → counterexample → fix` loop
over the neutral Core. Codex has full lifecycle hooks (`PreToolUse`,
`PostToolUse`, `Stop`, …) as well as `AGENTS.md` and `notify`, so — unlike
opencode — it can **enforce** the loop, not just prompt for it. The enforcing
gate here is a `PostToolUse` hook that verifies edited units and blocks on a
counterexample; `AGENTS.md` and `notify` back it up.

| File | Role |
|---|---|
| [`verify_hook.py`](./verify_hook.py) | **`PostToolUse` gate** — verifies `apply_patch` edits, blocks on VIOLATED |
| [`AGENTS.md`](./AGENTS.md) | Loop instructions Codex reads (embeds the #46 fallback block verbatim) |
| [`config.toml.example`](./config.toml.example) | Wires the hook + `notify`, registers the Core MCP server |
| [`notify.py`](./notify.py) | Secondary `notify` reminder at turn end (log + desktop notification) |

## Install

1. **Install the Core** (the hook uses the SDK-free `forseti verify` CLI; the
   `mcp` extra additionally exposes `forseti mcp` for the model to call `verify`
   itself):

   ```bash
   pip install 'forseti[mcp]'
   ```

2. **Give Codex the loop instructions.** Copy `AGENTS.md` to your project root,
   or merge its contents into an existing `AGENTS.md`.

3. **Wire the hook + notify + MCP.** Merge `config.toml.example` into
   `~/.codex/config.toml`, replacing the placeholder script paths with **absolute**
   paths to `verify_hook.py` and `notify.py`. (`codex mcp add forseti -- forseti
   mcp` registers just the MCP server.) Note: `notify` is a top-level key and must
   sit *before* any `[table]`/`[[table]]` header, or TOML scopes it into that
   table and Codex ignores it.

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
