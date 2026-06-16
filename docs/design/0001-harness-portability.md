# Design RFC 0001 — Harness portability & the loop protocol

- **Status:** Draft / RFC (thinking aid — not yet an ADR)
- **Date:** 2026-06-16

## Problem

The Forseti loop must run inside **multiple agent harnesses** — Claude Code, Codex, and
opencode — without rewriting the logic three times. Each harness has a different extension
model:

| Harness | Triggers / extension points | Tool access |
|---|---|---|
| **Claude Code** | hooks (PreToolUse/PostToolUse/Stop), subagents, skills, slash commands, plugins | MCP, CLI |
| **Codex** | `AGENTS.md`, `notify` hook (limited) | MCP, CLI |
| **opencode** | plugin API, custom commands, agents/modes — **no tool-use hooks** | MCP, CLI |

Hooks differ everywhere; the one substrate **all three share is MCP (+ a plain CLI).**

## Strawman: neutral core + thin adapters

Push *all logic* into a **harness-neutral Forseti Core**, and keep each harness's glue thin.

- **Forseti Core** (write once): the ESBMC wrapper, the property proposer, the loop logic, and
  the property store — exposed as a **CLI** and an **MCP server**.
- **Per-harness adapters** (thin): translate that harness's *triggers* into Core calls.
  - **Claude Code** — a **fork of the existing `esbmc-plugin`** (kept downstream, like the ESBMC
    fork): a `PostToolUse` hook that verifies after edits, a `Stop` hook that gates "done" on a
    proof, a **property-generation subagent**, and a skill/slash-command. All call Core.
  - **Codex** — `AGENTS.md` instructions + Core registered as an MCP server + a `notify` hook.
  - **opencode** — **no tool-use hooks**, so it uses the *prompt+tools fallback*: a custom
    command / subagent drives Core via MCP and emulates the Stop-gate in its own instructions.
    Same Core, weaker enforcement.

> **The hook is just the *trigger/gate*. The agent is the *worker*. The Core is the *tool*.**
> Where a harness lacks a given hook, it degrades gracefully to the agent calling Core tools
> directly from its prompt — same Core, weaker enforcement.

```mermaid
flowchart TB
    subgraph H["Harness adapters (thin)"]
      CC["Claude Code<br/>forked plugin: hooks + subagent + skill"]
      CX["Codex<br/>AGENTS.md + MCP + notify"]
      OC["opencode<br/>plugin + MCP"]
    end
    subgraph Core["Forseti Core — harness-neutral (CLI + MCP)"]
      L["Loop trigger / orchestration"]
      P["Property proposer<br/>(LLM / subagent)"]
      W["ESBMC wrapper<br/>VERIFIED | VIOLATED+cex | UNKNOWN"]
      DB[("Property store")]
    end
    CC -->|MCP / CLI| Core
    CX -->|MCP / CLI| Core
    OC -->|MCP / CLI| Core
    L --> P --> DB
    L --> W --> ESBMC[("ESBMC (forked)")]
    W --> DB
```

## One turn of the loop (protocol)

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent (CC / Codex / opencode)
    participant T as Trigger (hook / subagent / Stop-gate)
    participant F as Forseti Core
    participant S as Property store
    participant E as ESBMC (forked)

    A->>A: write / edit code unit U
    T->>F: verify(U)
    F->>S: get properties for U
    alt no properties yet
        F->>P: propose properties for U
        F->>E: grade (differential mutation-kill)
        E-->>F: kill-rates
        F->>S: persist graded properties
    end
    F->>E: verify(U, properties)
    alt VIOLATED
        E-->>F: counterexample (input + path)
        F-->>A: counterexample
        A->>A: fix U
        Note over A,F: loop repeats from the top
    else VERIFIED (up to k)
        E-->>F: proof
        F->>S: persist verdict + provenance
        F-->>T: pass → Stop-gate allows "done"
    else UNKNOWN
        E-->>F: timeout / k too small
        F-->>A: raise k / simplify / report
    end
```

## Loop control (decided direction)

Control flow is **hook-triggered, agent-as-worker**, with a fallback where hooks don't exist:
- Where tool-use hooks exist (**Claude Code**; **Codex** via its limited hooks/notify), a hook
  auto-runs `verify` after edits and a **Stop-gate** blocks "done" until the unit is VERIFIED.
- **opencode has no tool-use hooks** → **prompt+tools fallback**: a custom command / subagent
  tells the model to call `verify` after writing and keep fixing until it passes. Weaker
  *enforcement*, identical *Core*.

The Core is the same everywhere; only the trigger differs.

## Observability (required from day one)

A loop spanning hooks, an agent, the Core, and ESBMC is undebuggable without a **structured
event log**. Every step in the sequence diagram emits a JSONL event to a per-session trace:
`trigger.fired`, `core.verify.start`, `esbmc.invoke` / `esbmc.verdict`, `counterexample`,
`fix.attempt`, `stopgate.decision`, `property.proposed` / `property.graded`. One trace = one
replayable story of what the system did and why, across any harness. (Roadmap **W10**.)

## The property store — what it's actually *for*

Three different jobs get lumped together here. Pulled apart:

1. **Verdict cache (speed).** Key = `hash(unit text + property + esbmc-version + config)` → verdict.
   ESBMC is deterministic for fixed input, so if the *exact* pair recurs we skip the expensive run.
   **This is the "the agent sent the same code twice" case** — pure optimization, auto-invalidated
   when the code or property changes (different hash).
2. **Spec registry (intent across edits).** Key = stable **unit id** (`path::symbol`) → the set of
   properties we *intend* to hold + their grades. This **survives edits**: when the agent rewrites
   `rb_push`, we re-check the same intended properties rather than regenerating them (slow +
   non-deterministic) every turn.
3. **Proof-carrying record (shipping).** Serialized `unit → properties + last verdict + provenance`
   — the artifact that travels *with* the code (the deck's packaging open question).

They share storage but are keyed differently (cache by content-hash, registry by unit-id).

**Low-regret path (recommended) — don't build a DB yet:**
- **Spec registry + proof-carrying** = in-repo files keyed by unit id (`.forseti/<unit>.yaml`):
  versioned, diffable, portable, proof-carrying by construction.
- **Verdict cache** = a local content-hash store (a dir or a tiny SQLite), *derived/ephemeral*,
  not committed; its key **must include the ESBMC version**.
- **Analytics DB** = deferred until **GEPA (P2/P3)** actually needs corpus-wide kill-rate queries;
  then add a derived index rebuilt from the files. Measure before building.

## Still open (then these become ADRs)

- **Stop-gate strictness** — block hard on VERIFIED, or allow "VERIFIED-up-to-k with a flagged
  residual" so an UNKNOWN doesn't deadlock the agent.
- **Cache scope** — per-repo only vs a shared cross-project cache (and trusting a shared cache
  across ESBMC versions — hence the version in the key).
- ~~Unit granularity~~ — **decided: function/symbol level (`path::symbol`)**.
