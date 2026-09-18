# demo/scaffold

Reusable pieces for standing up an isolated demo workspace for Forseti's
`write -> verify -> counterexample -> fix` loop, driven by a real `claude`
session. Nothing here writes or runs the demo program itself, and nothing
here reads from this repo's `examples/` corpus — a Claude session started in
the generated workspace must not be able to see any pre-staged bugs.

## Files

- `CLAUDE.md` — the workflow contract copied into every generated demo
  workspace. It tells Claude to write the requested program and explains the
  installed verify-gate hooks: routine edits are checked automatically, but a
  function taking a pointer/array parameter only ever gets a non-blocking
  `needs_contract` from the hooks, so Claude must run `forseti synth --function
  <name> <file>` itself and keep fixing/re-running until the verdict is no
  longer `violated`. Once a function reaches `assumed_verified`/
  `discharged_verified`, it also has Claude run `forseti semantic-loop
  --mode propose` on it — the LLM-proposed-invariant path, checked by ESBMC
  in the same command — and report the outcome honestly (a `violated`
  semantic property may be a wrong guess, not a real bug; see the file for
  the exact judgment call). It deliberately says nothing about what kind of
  bug to expect.
- `init.sh` — materializes a fresh workspace at a target directory: creates
  the directory, `git init`s it with an initial empty commit (so Forseti's
  git-based out-of-band scan has a HEAD), copies in `CLAUDE.md`, and runs
  `forseti enable-project --harness claude-code` to install the
  SessionStart/PostToolUse/Stop hooks into
  `<target-dir>/.claude/settings.local.json`.

## Usage

```sh
source demo/env.sh   # puts this checkout's own forseti build on PATH
demo/scaffold/init.sh /path/to/some/scratch/dir
```

Requires `forseti` on PATH; the script checks this up front and fails loudly
(non-zero exit, clear message) rather than silently producing a workspace
with no verify-gate hooks. Use `demo/env.sh` (see `../env.sh`), not whatever
`forseti` a plain `pip`/`uv tool install` happens to have put on PATH
already — that separate install can lag behind this checkout by however many
merged-but-unreleased fixes.

Run it once per demo attempt — expect ~5-6 throwaway runs while empirically
picking the demo program/bug, plus one final run for the recorded take. Each
run is independent and safe to point at a different (or the same) directory;
it does not touch this repo's own working tree or its `examples/` directory.

After running, `cd` into `<target-dir>` and start a `claude` session there
with the actual demo prompt (a separate piece of this project, not built by
this scaffold).
