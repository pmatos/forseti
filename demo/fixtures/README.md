# demo/fixtures

A real, recorded run of the demo loop — not staged, not mutated. Sonnet 5,
running headless (`claude -p`) in a scaffolded workspace (`demo/scaffold/`,
built with this checkout's own `forseti` per `demo/env.sh`), was asked to
write a UTF-8 decoder with no hint about bugs or verification. This is the
`.forseti/events.jsonl` trace from that session, plus the generated source.

## Files

- `recorded_run.jsonl` — the full event trace (32 events): `write utf8.c` →
  the safety-gate hook reports `needs_contract` (pointer-taking function,
  non-blocking) → `forseti synth` reports `VERIFIED` (memory-safe, assumed) →
  `forseti semantic-loop --mode propose` asks an LLM for a semantic
  invariant, gets one, and checks it → `UNKNOWN` at k=16. The session raised
  the bound to k=64 by hand and got the same `UNKNOWN` in under a second,
  correctly diagnosing it as a harness ceiling (issue #299 — at the time,
  `semantic-loop` had no `--max-len` equivalent to bound an unconstrained
  length, unlike `synth`; it has since gained one) rather than a search that needed more time, and reported the
  unresolved result honestly rather than treating it as a pass. Local
  scratch-directory paths are redacted to `/demo/workspace`.
- `recorded_run.c` — the generated `utf8.c`, for context (not itself
  asserted against beyond what's already captured in the trace).
- `assert_recorded_run.py` — a standalone, stdlib-only e2e assertion script
  (not part of `forseti`'s own pytest suite — demo-only tooling, same as
  `demo/render.py`/`demo/canvas/server.py`) that checks the trace has the
  expected shape: write → needs_contract → synth VERIFIED → propose → check
  → UNKNOWN, and that nothing in the whole trace reports `violated` or a
  blocking `stop` decision (this trace is honest, not a forced failure).

## Usage

```sh
python3 demo/fixtures/assert_recorded_run.py            # asserts recorded_run.jsonl
python3 demo/fixtures/assert_recorded_run.py some/other/trace.jsonl

python3 demo/render.py --replay demo/fixtures/recorded_run.jsonl
python3 demo/canvas/server.py --replay demo/fixtures/recorded_run.jsonl
```

This is the rehearsal/fallback fixture for the demo: if a live run doesn't
cooperate, replay this real trace instead of faking one.
