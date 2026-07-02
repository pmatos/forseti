# Forseti harness adapters

The Forseti loop (`write → verify → counterexample → fix`) must run inside
several agent harnesses without rewriting the logic per harness. Per
[RFC-0001](../docs/design/0001-harness-portability.md), all logic lives in a
**harness-neutral Core** (exposed as the `forseti` CLI and an MCP server, #49),
and each harness gets a **thin adapter** that translates its own triggers into
Core calls.

> **The hook is the *trigger/gate*. The agent is the *worker*. The Core is the
> *tool*.** Where a harness lacks a hook, it degrades to the agent calling Core
> tools directly from its prompt — same Core, weaker enforcement.

## What lives here

| Path | Harness | Trigger model |
|---|---|---|
| [`prompt-tools-fallback.md`](./prompt-tools-fallback.md) | *any hookless harness* | reusable prompt: the agent drives Core via MCP (#46) |
| `codex/` | Codex | `AGENTS.md` + Core-as-MCP + `notify` hook (partial gate) — #47 |
| `opencode/` | opencode | custom command/subagent + Core-as-MCP, **no hooks** — #48 |

The **Claude Code** adapter is intentionally *not* here: per RFC-0001 it is a
downstream **fork of the `esbmc-plugin`** (kept downstream like the ESBMC fork),
with a `PostToolUse` verify hook, a `Stop`-gate, and a property subagent — see
issue #45. It is the reference "hooks enforce the gate" adapter; everything here
is about the harnesses where that enforcement is weaker or absent.

## Enforcement levels

The Core is identical everywhere; only the **trigger** and therefore the
**strength of the gate** differ.

| Harness | `verify` after edit | Stop-gate ("done" blocked until VERIFIED) | Enforcement |
|---|---|---|---|
| Claude Code (#45) | `PostToolUse` hook | `Stop` hook | **Hard** — the harness runs it |
| Codex (#47) | prompt; `notify` as a partial signal | prompt-emulated | **Partial** |
| opencode (#48) | prompt | prompt-emulated | **Soft** — instructions only |

## The prompt+tools fallback contract

When a harness cannot *force* verification with a hook, the adapter falls back to
the [reusable instructions](./prompt-tools-fallback.md), which tell the agent to
call Core `verify` after each edit and keep fixing until the unit passes. This
contract states exactly what that buys — and what it does not.

**Guarantees (behaviour the fallback defines):**

- **Same Core, same verdicts.** The agent calls the *same* `verify` operation
  over MCP that a hook would call; the verdict shape (`verified` / `violated` +
  `counterexample` / `unknown` / `error`) and its meaning are identical.
- **UNKNOWN is never a silent pass.** The instructions treat `unknown` as a
  distinct, non-passing state — raise k, simplify, or report — mirroring the
  hooked path's discipline. `verified` always means "verified **up to k**", never
  a general proof.
- **A single source of truth.** Every hookless adapter embeds or references the
  one `prompt-tools-fallback.md` block, so the loop's wording does not drift per
  harness.

**Non-guarantees (the cost of having no hook):**

- **The gate is advisory.** Nothing *forces* the agent to call `verify` or to
  respect the Stop-gate; enforcement is only as strong as the model's adherence
  to the prompt. A hook cannot be bypassed by the model — a prompt can.
- **No out-of-band interception.** Edits made outside the agent's own tool calls
  (e.g. a side channel the prompt never sees) are not verified, because there is
  no `PostToolUse`-style trigger observing them.
- **Best-effort convergence.** Without a gate to block "done", a run can end with
  an unverified unit if the model ignores the instructions. The mitigation is to
  make the residual *loud* (report which unit/property/k), not to pretend it
  passed.

The upshot: prefer the hooked Claude Code adapter (#45) where enforcement
matters; use this fallback where the harness gives you no hook to enforce with,
and read a `verified` from it as "the agent says it verified up to k", not "the
harness guaranteed it".
