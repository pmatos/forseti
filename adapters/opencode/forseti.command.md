---
description: Run the Forseti verify → fix loop on a unit until it is VERIFIED up to k
agent: forseti-verify
subtask: true
---
<!--
Forseti — opencode command. Place this file at:
  .opencode/commands/forseti.md            (per-project)
  ~/.config/opencode/commands/forseti.md   (global)
Invoked as `/forseti <file-or-unit>`. `subtask: true` forces it into the
forseti-verify subagent so the loop runs off your primary context.
-->

Run the Forseti verification loop on: $ARGUMENTS

Verify that unit with the `forseti` MCP `verify` tool, fix from any
counterexample, and repeat until it is `verified` up to k. Do not report the
work complete while any changed unit is `violated` or `unknown` — surface the
residual instead.
