# Codex adapter

Makes **Codex** drive the Forseti `write → verify → counterexample → fix` loop
over the neutral Core. Per [RFC-0001](../../docs/design/0001-harness-portability.md),
Codex has `AGENTS.md` and a limited `notify` hook, but **no tool-use hook** that
can block a turn — so enforcement is the [prompt+tools fallback](../prompt-tools-fallback.md)
with `notify` as a **partial** (post-turn, non-blocking) gate.

| File | Role |
|---|---|
| [`AGENTS.md`](./AGENTS.md) | Loop instructions Codex reads (embeds the #46 fallback block verbatim) |
| [`config.toml.example`](./config.toml.example) | Registers the Core MCP server + wires `notify` |
| [`notify.py`](./notify.py) | Reference `notify` hook — surfaces a reminder at turn end |

## Install

1. **Install the Core with the MCP extra** (exposes `forseti mcp`, #49):

   ```bash
   pip install 'forseti[mcp]'
   ```

2. **Give Codex the loop instructions.** Copy `AGENTS.md` to your project root,
   or merge its contents into an existing `AGENTS.md`.

3. **Register the Core as an MCP server.** Merge `config.toml.example` into
   `~/.codex/config.toml`, or run:

   ```bash
   codex mcp add forseti -- forseti mcp
   ```

4. **(Optional) Wire the partial gate.** Point Codex's `notify` at `notify.py`
   using an **absolute** path (see `config.toml.example`).

## Enforcement level: partial

- `AGENTS.md` instructs the agent to call `verify` after edits and to keep fixing
  until VERIFIED — but nothing *forces* it (a prompt can be ignored; a hook
  cannot). This is the fallback's [documented non-guarantee](../README.md).
- `notify` fires only at `agent-turn-complete`, **after** the turn, so it cannot
  block a hand-off — it just makes an unverified/`unknown` residual visible. For
  a hard gate (`PostToolUse` verify + `Stop`), use the Claude Code adapter (#45).
