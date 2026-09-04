# Oh My Pi adapter (#249)

Makes [Oh My Pi](https://github.com/can1357/oh-my-pi) drive Forseti Core's
`write → verify → counterexample → fix` loop over the neutral Core, the same
way the Claude Code and Codex adapters do (#213). This adapter owns **no**
ESBMC invocation or harness-generation logic of its own — it only consumes
the Core CLI (`forseti list-units`, `forseti semantic-loop`).

Oh My Pi's own extension API is genuinely different from the other two
harnesses: it has native TypeScript extensions with a `tool_call`
(pre-execution, can block) / `tool_result` (post-execution, can override the
result) pair, project-level `.omp/` configuration, and first-class MCP
support (`.omp/mcp.json`). By the time `tool_result` fires the edit has
already landed on disk — the same as Codex's `PostToolUse` — so there is
nothing left to *block*; instead this gate rewrites the tool's own result so
the model sees the counterexample as part of what it just did. Verified
against Oh My Pi's current `ExtensionToolWrapper` source
(`packages/coding-agent/src/extensibility/extensions/wrapper.ts`, not the
legacy `HookToolWrapper` its own `docs/hooks.md` describes): a `tool_result`
handler's returned `isError` **is** applied, so this gate reads to the model
as the edit itself having failed — comparable in strength to Codex's
`{"decision": "block", ...}`, not merely an FYI aside.

| File | Role |
|---|---|
| [`verify_hook.py`](../../src/forseti/adapters/oh_my_pi/verify_hook.py) | **`tool_result` gate** — checks a `write`/`edit`'s stored semantic properties, blocks (`isError: true`) on VIOLATED. Ships inside the `forseti` package (#249), dispatched via `forseti omp-hook tool-result` |
| [`install.py`](../../src/forseti/adapters/oh_my_pi/install.py) | `forseti enable-project --harness oh-my-pi`'s install/update logic for `.omp/extensions/forseti-gate.ts` + `.omp/mcp.json` |
| [`forseti-gate.ts`](./forseti-gate.ts) | Reference copy of the native extension `install.py` writes (pinned equal by `test_omp_install.py`) |

## Install

```bash
pip install 'forseti[mcp]'
forseti enable-project --harness oh-my-pi /path/to/your/project
```

This writes (or idempotently updates) two things in the target project:

1. `.omp/extensions/forseti-gate.ts` — the gate itself. Whole-file, not
   merged: a project with a hand-written `forseti-gate.ts` (no forseti
   sentinel header) is left untouched and `enable-project` refuses to
   overwrite it, rather than risk clobbering unrelated content.
2. `.omp/mcp.json`'s `mcpServers.forseti` entry (`{"command": "forseti",
   "args": ["mcp"]}`), registering the Core MCP server so the driving model
   can call `semantic_loop`/`check`/`propose`/`submit` itself — the same
   host-model property-generation path Codex's `AGENTS.md` documents,
   wired through Oh My Pi's own native MCP config instead of prose
   instructions. Every other key in `mcp.json` (`$schema`,
   `disabledServers`, other servers) survives untouched.

`forseti disable-project --harness oh-my-pi` removes both, again refusing to
touch either one if it no longer looks like forseti's own (e.g. a
hand-edited `mcpServers.forseti` entry whose `command` isn't `"forseti"`).
`--harness oh-my-pi` cannot be auto-detected — unlike Codex/Claude Code, Oh
My Pi sets no session-identifying environment variable — so `enable-project`
always needs it passed explicitly here.

## Enforcement level: hook-enforced, no completion gate

- **`tool_result` (`verify_hook.py`) is the gate.** After a `write`/`edit` it
  runs `forseti list-units` to find the edited file's functions, then
  `forseti semantic-loop --mode check-only` (checks what's already
  proposed/submitted — this gate never proposes new candidates itself) for
  each. A **violated** verdict rewrites the tool's own result with the
  counterexample and sets `isError: true`.
- **unknown / error are surfaced, not blocked.** Reported inline in the same
  way, without `isError` — consistent with every other adapter's "never
  silently pass UNKNOWN" contract.
- **Store-presence opt-in.** No `.forseti/forseti.db` in the project means
  zero subprocess cost — a project that never `propose`s/`submit`s a
  property stays at today's behaviour, no flag needed (mirrors the Claude
  Code adapter's own `property_gate.py`).
- **No completion/Stop-gate.** Oh My Pi's documented event surface
  (`docs/extensions.md`, `docs/hooks.md`) has no analogue to Claude Code's
  `Stop` hook or a way to block turn/session completion — `session_shutdown`,
  `turn_end`, and `agent_end` are not documented as cancelable. A VIOLATED
  semantic property is therefore reported at every edit but cannot suspend
  "done" the way Claude Code's Stop-gate does; Oh My Pi's enforcement level is
  therefore **Medium**, the same tier as Codex (post-edit block only, no
  completion gate) — honestly, not a faked parity with Claude Code's
  post-edit-block-**and**-completion-gate Strong tier.
