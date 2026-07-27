# Forseti — Claude Code adapter (v0: safety verify-gate)

A **self-contained** Claude Code plugin that puts ESBMC inside the coding loop as
a *hard gate*. It has **no dependency on the `esbmc-plugin`** and needs no MCP
server — the hooks call the neutral `forseti` CLI directly.

> **Forseti returns a verdict; the harness owns the loop.** The hooks are the
> trigger/gate, Claude is the worker, the `forseti` CLI is the tool. Forseti
> itself never loops — each call verifies once and returns `VERIFIED (up to k) |
> VIOLATED + counterexample | UNKNOWN | ERROR`.

## What it does

- **PostToolUse hook** — after every `Write`/`Edit`/`MultiEdit` of a `.c`/`.h`
  file, it verifies each top-level function defined in that file at the
  **function level** (`esbmc --function <name>`): no `main`, no harness. ESBMC
  havocs the parameters and checks the built-in **safety** properties (memory
  safety, signed overflow, array bounds, division by zero, UB). A non-`VERIFIED`
  verdict is fed straight back to Claude as the counterexample to fix — **except**
  a unit that takes a **pointer/array parameter**, which is reported
  `NEEDS_CONTRACT` and *not* gated (see the note under **Known limitations**).
- **PostToolUse `Bash` hook** — a C file written *out-of-band* via the `Bash`
  tool (`cat > f.c`, a generator script, `sed -i`, a heredoc) carries a `command`
  string, not a `file_path`, so it never triggers the edit hook above. After every
  `Bash` call this hook asks `git` which C sources changed and verifies each one
  whose content differs from what the gate last saw — the same function-level
  ESBMC pass, feeding any counterexample back the same way. It never parses the
  shell command for filenames (unreliable); discovery is the union of `git status`
  (working-tree/index changes) and C **committed since the session baseline HEAD**,
  so a command that writes *and* commits a C file in one shot (`cat > f.c && git
  commit …`, leaving a clean worktree) is still caught. Content-hash freshness is
  the real gate, so a file merely committed *unchanged* is deduped back out and
  untouched third-party C is skipped — save for C a HEAD movement
  (`checkout`/`merge`/`rebase`) sweeps in, a deliberate over-gate noted under
  **Known limitations**. Requires the project to be a **git repository**.
- **SessionStart hook** — records the content of every C file already dirty at
  session start as the *baseline*, plus the baseline HEAD commit, so the
  out-of-band scan gates only C the agent changes **during** the session (whether
  left dirty or committed), never pre-existing WIP it never touched. Without it a
  `git status` scan would flag the user's uncommitted C on the very first turn.
- **Stop hook** — blocks the turn from ending while any touched unit is not
  `VERIFIED up to k`. As an ESBMC-free backstop it also re-checks `git` for C
  files changed out-of-band that are still unverified, and blocks on those too —
  so the heavy verification stays in the 300 s PostToolUse budget, never in the
  kill-sensitive Stop hook. Freshness compares the last-verified content against
  the worktree copy **and** the staged (index) and committed (`HEAD`) blobs, so a
  Bash command that stages or commits a divergent blob and then reverts the
  worktree — leaving it hashing clean while unverified C sits in the index/`HEAD`,
  ready to ship — is still caught (issue #99 review); the block spells out the
  index/commit-shaped remediation (`git add`/`git restore --staged`), since
  editing the worktree cannot reconcile a staged blob. After `MAX_STOP_ATTEMPTS`
  (3) consecutive blocks with no fix, it lets the turn end but with a **loud**
  unverified residual — never a silent pass, never an infinite loop.

Latest verdicts are cached in `.forseti/gate_state.json` (per project,
gitignored). Forseti core stays stateless; the *gate* is what is stateful. The
full ordered history of the loop — every hook firing, ESBMC call, and gate
decision — is appended to `.forseti/events.jsonl` (see **Loop trace** below).

### Scope: v0 = safety, v1 = semantics

A harness is only needed to express a **contract you invented** ("the output is
sorted", "abs(x) ≥ 0"). Language-level **safety** properties are free at the
function level — that is all v0 checks. Generated *semantic* properties (propose
→ render harness → check) are **v1**, not wired here yet.

## Requirements

- `esbmc` on `PATH` (the gate shells out to it via Forseti).
- The `forseti` CLI on `PATH`: from the Forseti repo, `pip install -e .` (the
  hooks fall back to `python -m forseti.core` if the package is importable but
  the script is not on `PATH`).
- `git` on `PATH`, and the target project a git repository — required only for
  gating out-of-band `Bash` writes; the edit-triggered gate works without it.

## Enable it

Hooks load at **session start**, so after either method, **restart Claude Code**
(`claude`), then confirm with `/hooks`.

**As a plugin (recommended, portable):** install this directory as a plugin (via
your marketplace, or point Claude Code at `adapters/claude-code/`). The
`hooks/hooks.json` wires both hooks using `${CLAUDE_PLUGIN_ROOT}`.

**As project settings (no plugin):** add to the target project's
`.claude/settings.json`, replacing `ABS_PATH` with the absolute path to this
directory:

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "*",
        "hooks": [{ "type": "command", "command": "python3 \"ABS_PATH/hooks/session_start.py\"", "timeout": 60 }] }
    ],
    "PostToolUse": [
      { "matcher": "Write|Edit|MultiEdit",
        "hooks": [{ "type": "command", "command": "python3 \"ABS_PATH/hooks/post_tool_use.py\"", "timeout": 300 }] },
      { "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "python3 \"ABS_PATH/hooks/post_bash.py\"", "timeout": 300 }] }
    ],
    "Stop": [
      { "matcher": "*",
        "hooks": [{ "type": "command", "command": "python3 \"ABS_PATH/hooks/stop_gate.py\"", "timeout": 120 }] }
    ]
  }
}
```

## Try the demo

In a C project with the plugin enabled, ask Claude:

> *Implement `int64_t my_abs(int64_t x)` that returns the absolute value, in
> `abs64.c`.*

Claude writes the obvious `(x < 0) ? -x : x`. The PostToolUse hook verifies
`abs64.c::my_abs` and returns **VIOLATED** with the counterexample `x =
INT64_MIN` (`arithmetic overflow on neg`, CWE-190/191). Claude reads it, saturates
`INT64_MIN → INT64_MAX`, and the re-verify returns **VERIFIED up to k**. Only then
does the Stop-gate let the turn end. See
[`docs/walkthroughs/0002-hook-enforced-safety.md`](../../docs/walkthroughs/0002-hook-enforced-safety.md).

## Loop trace (understand the back-and-forth)

`gate_state.json` is a *snapshot* (latest verdict per unit). To see the whole
`write → verify → cex → fix` sequence, the hooks also append an ordered event log
to **`.forseti/events.jsonl`** — one JSON object per line:

- `edit` — a `Write`/`Edit`/`MultiEdit`, or a `Bash` out-of-band write, fired: the tool, the file, the functions found.
- `verify` — one ESBMC call: the unit, `verdict`, `k`, `duration_s`, and the
  **`argv`** esbmc ran — with its source token naming the real file on disk,
  even though the gate actually verifies an immutable snapshot beside it
  (issue #150): the trace and any counterexample have to name something
  Claude can still read and fix.
- `gate` — the PostToolUse decision: `pass`, or `block` (how many cex were fed back).
- `stop` — the Stop-gate decision: `block`, loud `residual`, or `allow`.

Render it as a **mermaid sequence diagram** with the bundled tool (point it at the
project dir or the `events.jsonl` file):

```console
$ python3 adapters/claude-code/tools/trace_to_mermaid.py path/to/project
```

For the `my_abs` demo the one turn comes out as:

```mermaid
sequenceDiagram
    participant C as Claude
    participant G as Gate (PostToolUse)
    participant E as ESBMC
    participant S as Stop-gate
    C->>G: Write abs64.c (my_abs)
    G->>E: verify abs64.c::my_abs (k=1)
    E-->>G: VIOLATED (0.15s)
    G-->>C: block — 1 cex fed back (exit 2)
    C->>S: end turn?
    S-->>C: BLOCK (attempt 1)
    C->>G: Edit abs64.c (my_abs)
    G->>E: verify abs64.c::my_abs (k=1)
    E-->>G: VERIFIED (0.12s)
    G-->>C: pass — VERIFIED up to k
    C->>S: end turn?
    S-->>C: ALLOW (clean)
```

The trace captures Claude's **actions** (the code it writes/edits) and the
verifier's responses, not Claude's natural-language messages — those live only in
Claude Code's own session transcript (`~/.claude/projects/<slug>/<session>.jsonl`)
and can be woven in by timestamp. Logging is best-effort: a trace write never
turns a verdict into an error.

## Configuration

| Setting | Where | Default | Notes |
|---|---|---|---|
| Safety flags | `SAFETY_FLAGS` in `hooks/forseti_gate.py` | `--overflow-check` | bounds/pointer/div-by-zero are ESBMC defaults; unsigned-overflow left OFF (legal wraparound) |
| Unwind bound *k* | `FORSETI_UNWIND` env | `1` | a `VERIFIED` is only "up to k"; **loops need a higher k** |
| Verify timeout | `FORSETI_VERIFY_TIMEOUT_S` env | `110` | per-function budget, passed to `forseti verify --timeout` so ESBMC honors it (the subprocess is bounded ~15 s higher). Each verdict is persisted the moment it lands, so the `300` s PostToolUse hook timeout must stay above this per-function budget — raise both together for very slow units. |
| List-units timeout | `FORSETI_LIST_UNITS_TIMEOUT_S` env | `30` | budget for the one `forseti list-units` parse per edited file; a `--parse-tree-only` run does no solving, so this rarely needs raising |
| Build flags | `FORSETI_BUILD_FLAGS` env | *(none)* | the project's own `-I`/`-D`, shell-quoted (`-Iinclude -Ivendor/tls -DNDEBUG`). Forwarded to **both** `forseti list-units` and `forseti verify`, so the enumeration and the verify see the same translation unit. Set this if your C only compiles with include paths: without them ESBMC cannot resolve an `#include`, the enumeration fails, and every edited file blocks with an `error`. Unlike `SAFETY_FLAGS` these are build-time, not property-checking, flags. |
| Stop-gate attempts | `MAX_STOP_ATTEMPTS` in `forseti_gate.py` | `3` | blocks then lets the turn end with a loud residual |
| Out-of-band include | `FORSETI_GATE_INCLUDE` env | *(all C files)* | `:`/`,`-separated globs; if set, only changed C files matching one are scanned. A bare name (`src`) matches any path segment; a glob (`kernels/*.c`) matches the project-relative path. |
| Out-of-band exclude | `FORSETI_GATE_EXCLUDE` env | `third_party`, `vendor`, `node_modules` | same syntax; excludes win over includes. Setting it **replaces** the defaults. Git's own ignore rules already drop gitignored build output before this applies. |

## Known limitations (v0)

- **Function detection uses ESBMC's clang frontend** (`forseti list-units`), the
  same parser that verifies — so typedef'd pointers, K&R and multi-line
  signatures, function-like macros, `#if` blocks, and a `*` inside a comment are
  all classified correctly (no regex, issue #131). Each edited `.c` file gets one
  extra `--parse-tree-only` parse (no solving, fast); if enumeration fails (esbmc
  missing, C parse error) the file's units are recorded as a blocking ERROR
  verdict rather than silently skipped. A file that only parses with the
  project's include paths needs `FORSETI_BUILD_FLAGS` set — an unresolvable
  `#include` is such a failure. What gets parsed is an **immutable snapshot** of
  the exact bytes the gate hashed (issue #141), so a rewrite concurrent with the
  parse cannot make the gate enumerate one version of the file while stamping
  another. The snapshot is staged as a **sibling of the source, in its own real
  directory** (issue #151) — not a copy mirrored elsewhere — so it needs no
  reproduction of the source's neighbourhood at all: every real sibling, and
  everything a `..` chain can reach, resolves exactly as an in-place parse
  would, because the snapshot *is* in that directory. (Naming the directory with
  `-I` instead would be wrong: `-I` also joins the *angle-bracket* search and
  lands after any `-iquote`, so `#include <config.h>` next to a generated
  `config.h` would pick the wrong one, flip an `#if`, and hide a unit from the
  gate — same-directory staging needs no flag at all.) A snapshot's name is
  excluded from the gate's own discovery — never subject to
  `FORSETI_GATE_INCLUDE`/`_EXCLUDE` — for as long as git can show it is
  untracked, so one a killed hook could not clean up is never itself offered
  back as a source; a *tracked* file that happens to share the snapshot's
  basename prefix is still discovered and gated normally. Inside a git work
  tree the snapshot's name is also registered in `.git/info/exclude` before it
  is staged, so a concurrent `git add -A`/`git status` never sees it either —
  only an explicit, forced `git add -f <path>` can still index it. The trade for
  dropping the earlier mirrored design (which had a project-root boundary a
  quoted include could climb past, silently landing on a different translation
  unit) is a narrower residual: a translation unit that `#include`s the source
  under any name that resolves to its own inode — its literal filename, a
  same-directory symlink, or
  a hard link — still reaches the live file for that nested read, since a
  same-directory snapshot needs a random name to avoid colliding with a
  concurrent enumeration of the same file and so cannot occupy any of those
  names.
- **The verify step also runs on an immutable snapshot, not the real path
  (issue #150).** Every verdict is computed against content hashing to the
  digest `scanned` records, full stop — a transient `A → B → A` during the
  verify can no longer attach `B`'s verdict to `A`'s stamp, since the snapshot's
  bytes never move. The snapshot is staged as a **sibling of the source, in its
  own real directory** — the same design the enumeration snapshot above uses,
  for the same reason: no mirror root to fall off, so a quoted `#include`
  resolves exactly as the in-place parse would, with no `-I` approximation to
  get wrong. The trade is a narrower residual in its place: a translation unit
  that `#include`s **itself by its own literal name** still reaches the live
  file for that nested read, since a private snapshot cannot occupy the
  original's name (it needs a random one to avoid colliding with a concurrent
  verify of the same file). That random name would otherwise leak into
  `__FILE__`/`__BASE_FILE__` too — code that branches on its own presumed name
  (`strcmp(__FILE__, "expected.c")`, say) would take a different path than the
  real file does — so the snapshot's first line is a `#line 1 "<real path>"`
  directive, fixing the presumed name back to the original without touching
  where its bytes sit; verified against a live `esbmc` run. The snapshot's name
  is excluded from the gate's own discovery — never subject to
  `FORSETI_GATE_INCLUDE`/`_EXCLUDE` — for as long as git can show it is
  untracked, so one a killed hook could not clean up is never itself offered
  back as a source to verify; a *tracked* file that happens to share the
  snapshot's basename prefix is still discovered and gated normally. Staging it
  at all is skipped when every definition in the file takes a pointer/array
  parameter (`NEEDS_CONTRACT`, no ESBMC call ever reached): a directory that
  could not host a snapshot must not gate an edit that would never have needed
  one. Every path the CLI's own response names — the trace's
  `argv` and any counterexample — is rewritten from the snapshot back to the
  real file before it is recorded, so the loop trace and any fix Claude is asked
  to make still point at a file that exists on disk. The file is still
  re-hashed once after the verify loop, but that check now exists only for
  *promptness*, not correctness: a rewrite that lands and **stays** (A → B)
  after the snapshot was taken is nobody's fault, and the out-of-band scan would
  eventually re-gate `B` on its own — but there is no such scan outside a git
  work tree, and even inside one it only runs on the next hook, so the run
  withdraws its `scanned` stamp and records a blocking ERROR rather than wait.
  Taking that stamp is itself ownership-scoped: the re-hash happens under the
  same lock that writes it, so concurrent hooks cannot interleave between the
  two, and a run whose bytes have already been superseded by another run's stamp
  defers to it silently instead of reclaiming the entry or blocking on a file
  that run legitimately verified.
- **Only `.c` translation units are verified; header definitions are out of
  scope.** ESBMC cannot parse a `.h` standalone (`forseti verify`/`list-units`
  both error with "failed to figure out type of file"), and a function defined in
  an `#include`d header is attributed to the header, not its includer — so it is
  not gated either way. A `.h` edit is a clean pass (nothing enumerated). This
  trades the old regex's behaviour, which errored on a header that happened to
  contain a definition; you can't verify what ESBMC won't parse.
- **No k-escalation.** The gate verifies at one fixed k; an `UNKNOWN` (e.g. a
  loop under-unwound) blocks with guidance to raise `FORSETI_UNWIND`, rather than
  laddering k automatically.
- **Pointer/array units are not gated yet (`NEEDS_CONTRACT`).** At the function
  level ESBMC passes a pointer parameter an *unconstrained* value (object identity
  + offset over the whole object universe, including the invalid object), so any
  `*p`/`p[i]` yields a **sound but unactionable** `dereference failure` — the code
  isn't wrong; the caller-side memory precondition is simply absent. Rather than
  feed that phantom back as a fixable counterexample (which made correct code loop
  forever), a unit with a pointer/array parameter is classified `NEEDS_CONTRACT`
  by **signature** (never by matching the cex text — a real out-of-bounds prints
  the same string): the ESBMC run is skipped, the unit is **not** gated, and it is
  reported loudly but non-blocking. Actually verifying these — by generating a
  memory precondition/harness — is [#122](https://github.com/pmatos/forseti/issues/122)
  (design in [RFC-0003](../../docs/design/0003-memory-preconditions.md)).
- **Safety only.** Functional correctness beyond the built-in safety checks is
  the v1 semantic-property path.
- **Very slow, many-function files.** Verdicts persist incrementally so a hook
  kill can't cause a silent pass, but a file whose *total* verification exceeds
  the PostToolUse hook timeout can have its last, still-running function cut off
  before its verdict lands. The scan retries such a file — an interrupted verify is
  recorded as unfinished, so the file is re-verified on the next scan even though
  its content hash is unchanged ([#140](https://github.com/pmatos/forseti/issues/140)) —
  but only 3 times per unchanged content: a file that can *never* finish inside the
  budget would otherwise reset the Stop-gate's patience every round and loop
  forever. After that its pending units block their way to the loud residual.
  Raise the hook timeout (and `FORSETI_UNWIND` budget) for such files.
- **Out-of-band gating needs a git repo.** C files written via the `Bash` tool
  are gated by a `git status`-scoped scan (the `Bash` PostToolUse hook, plus the
  Stop-gate backstop). In a project that is **not** a git repository that scan is
  inactive — a Bash-written C file there is not verified. The degraded scope is
  recorded in the trace (`oob_scan_skipped`) rather than passing silently, but it
  is not gated. Scope is **"C changed since session start"** (issue
  [#99](https://github.com/pmatos/forseti/issues/99)): the SessionStart hook
  baselines the already-dirty tree — its worktree bytes *and* its staged/`HEAD`
  blobs, so a session opening at `MM foo.c` (staged WIP, worktree reverted) is not
  blocked on the user's own pre-session index, issue
  [#139](https://github.com/pmatos/forseti/issues/139) — *and* the current HEAD, so
  the scan catches this session's Bash writes — including a C file written **and
  committed in one shot** (a clean worktree `git status` alone would miss),
  recovered by diffing the baseline HEAD against the current one — while never
  gating pre-existing uncommitted or committed/third-party C the agent never
  touched. Two documented bounds of the HEAD-diff: (1) a HEAD movement that
  brings in C without the agent authoring it (a `git checkout`/`merge`/`rebase`
  run via Bash) is conservatively gated — an over-gate that blocks loudly, never
  a silent pass; (2) in a repo with **no commits** at session start there is no
  baseline HEAD, so the very first commit's C is caught only if it is also left
  dirty. It relies on content changes git can see; a change git cannot (a file
  outside the work tree, or one matched by `.gitignore`) is not scanned. A file
  changed *between* sessions is re-baselined on the next fresh start, so it is
  treated as pre-existing rather than gated.
- **Staged/committed-blob freshness is single-hash and content-literal.** The gate
  records one last-verified hash per file, so if you stage a *previously* verified
  blob and then keep editing to a newer verified version, the now-superseded staged
  blob is conservatively gated until you re-stage or unstage it — an over-gate that
  blocks loudly, never a silent pass. Likewise the staged/committed blob is compared
  byte-for-byte, so a git content filter (`core.autocrlf`, a clean/smudge filter)
  that rewrites the blob relative to the worktree can over-gate. Both resolve by
  bringing the index/commit to the verified content (`git add`) and re-verifying.
  A blob the SessionStart baseline recorded stays exempt for the whole session —
  it is the user's pre-session WIP, unchanged — so leaving it staged, or committing
  it as-is, does not block; only *different* bytes reaching the index/HEAD gate.
- **mtime is not used.** Freshness is keyed on a SHA-256 of file content, so a
  `cp -p`/`tar` that preserves an old timestamp cannot slip an unverified change
  past the gate, and an unchanged file is never needlessly re-verified.
