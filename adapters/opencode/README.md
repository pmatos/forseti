# opencode adapter

Makes **opencode** drive the Forseti `write → verify → counterexample → fix`
loop over the neutral Core. Per [RFC-0001](../../docs/design/0001-harness-portability.md),
opencode has **no tool-use hooks at all**, so this is the purest
[prompt+tools fallback](../prompt-tools-fallback.md): a custom command routes to
a subagent that calls Core `verify` over MCP and emulates the Stop-gate in its
own instructions. Same Core, **soft** enforcement.

| File | Role | Copy to |
|---|---|---|
| [`forseti.command.md`](./forseti.command.md) | `/forseti <unit>` command (thin trigger) | `.opencode/commands/forseti.md` |
| [`forseti-verify.agent.md`](./forseti-verify.agent.md) | worker subagent (embeds the #46 loop block) | `.opencode/agents/forseti-verify.md` |
| [`opencode.json.example`](./opencode.json.example) | registers the Core MCP server | merge into `opencode.json` |

## Install

1. **Install the Core with the MCP extra** (exposes `forseti mcp`, #49):

   ```bash
   pip install 'forseti[mcp]'
   ```

2. **Register the Core as an MCP server.** Merge `opencode.json.example` into
   your project `opencode.json` (or `~/.config/opencode/opencode.json`). It
   declares a local (stdio) server `forseti` started by `forseti mcp`.

3. **Install the command + subagent.** Copy the two markdown files to the paths
   in the table above (per-project `.opencode/…`, or global
   `~/.config/opencode/…`). Filenames set the identifiers, so keep them:
   `forseti.md` → `/forseti`, `forseti-verify.md` → `@forseti-verify`.

4. **Use it.** Run `/forseti path/to/file.c` (or `@forseti-verify` in a message)
   to drive the loop on a unit.

## Enforcement level: soft

opencode offers no hook to *run* `verify` or to block "done" — the gate is
purely the subagent's instructions, the weakest of the three harnesses. A
`verified` here means "the subagent says it verified up to k", not "the harness
guaranteed it" (see the [fallback contract](../README.md)). For a hard
`PostToolUse`+`Stop` gate, use the Claude Code adapter (#45).

> Directory names follow the current opencode docs (`commands/`, `agents/`,
> plural). If your opencode version differs, place the files where its docs say
> commands and agents live — the file contents are unchanged.
