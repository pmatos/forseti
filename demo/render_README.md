# demo/render.py -- live events.jsonl sequence-diagram renderer

Turns `.forseti/events.jsonl` (the interleaved trace written by the Claude
Code adapter's hooks, the `demo/bin/forseti` shim, and Forseti Core's
`propose`/`check`) into a readable, incrementally-appended timeline. Meant to
run in one half of a split tmux/terminal layout, next to a real `claude`
session, as the "observability" pane.

Stdlib-only -- no pip dependencies -- matching this repo's dependency-free
philosophy for the loop path.

## Live mode

```sh
python3 demo/render.py <project_dir>
```

Tails `<project_dir>/.forseti/events.jsonl` as it grows. If the file doesn't
exist yet, it waits/polls for it. If the file is truncated or rotated (a new
demo run against the same path), it notices and resumes from the start
rather than hanging or crashing.

Stop with Ctrl-C.

## Replay mode

```sh
python3 demo/render.py --replay <trace-file>
python3 demo/render.py --replay <trace-file> --speed 1     # real-time pacing
python3 demo/render.py --replay <trace-file> --speed 4     # 4x fast-forward
```

Plays back a previously captured `events.jsonl`. Without `--speed`, it prints
instantly. With `--speed N`, the delay between consecutive events is the
recorded timestamp delta divided by `N` (so `1` is real time, higher is
faster), capped at 3 seconds per gap so a long real-session pause doesn't
stall the replay. This is the rehearsal/fallback mode for when a live demo
run doesn't cooperate.

## Other flags

- `--no-color` -- disable ANSI colors (colors are also auto-disabled when
  stdout isn't a terminal, or `NO_COLOR` is set).

## What gets rendered

One line per recognized event, prefixed with a dim `[HH:MM:SS]` timestamp:

```
[10:02:11] SESSION baseline (n_baselined=3, source=startup)
[10:02:16] AGENT wrote utf8.c (functions: utf8_decode)
[10:02:17] ESBMC verify utf8.c::utf8_decode -> NEEDS_CONTRACT (k=?, 0.05s) (gate: no harness for pointer/array param -- not evidence of safety)
[10:02:17] GATE pass utf8.c (needs_contract=1)
[10:02:21] CLI forseti synth utf8.c --function utf8_decode -> assessment: violated (exit 1, 0.42s)
[10:02:31] AGENT edited utf8.c (functions: utf8_decode)
[10:02:41] CLI forseti synth utf8.c --function utf8_decode --json -> assessment: assumed_verified (exit 0, 0.38s)
[10:02:42] STOP allow_needs_contract (needs_contract=1)
```

Colors: green = verified / pass / held; red = violated / block; yellow =
unknown / needs_contract / error (a deliberate choice, not a bug -- see
"Vocabulary discipline" below). `property.proposed` / `property.check.start`
/ `property.verdict` (Core's `propose`/`check` trace) render as `PROPOSE` /
`CHECK` lines.

The adapter-local `gate` event and Core's canonical `gate.decision` event for
the *same* edit are the same decision reported twice (RFC/issue #213); the
renderer collapses them into one `GATE` line. A `gate.decision` that doesn't
match a preceding local `gate` line (e.g. the Stop-gate's own semantic-check
residual, which carries no `file`) is shown on its own, tagged with its
adapter.

Malformed or unrecognized-`type` lines are skipped silently, never a crash --
this matches how the package's own `event_log.read_events_file` tolerates a
torn trailing write.

## Vocabulary discipline (hard rule, see this repo's root CLAUDE.md)

ESBMC never "proves" anything -- it returns a bounded verdict. This renderer
never prints "proven" or "correct"; it always prints
VERIFIED/VIOLATED/UNKNOWN (or NEEDS_CONTRACT/ERROR for the gate, or the
`forseti synth`/`discharge` assessment vocabulary), and UNKNOWN,
NEEDS_CONTRACT and ERROR are always styled yellow -- visually distinct from a
pass, never colored as if they were one.

Two different "needs_contract" claims appear in the trace and are kept
distinct rather than conflated:

- A `verify` event's `needs_contract` **verdict** -- the safety gate couldn't
  check a pointer/array-taking function without a synthesized harness. This
  is non-blocking, and it is *not* evidence the function is safe.
- A `cli` event's own `assessment` field (parsed best-effort out of its
  `output_tail`, since `forseti synth`/`discharge` have no dedicated event
  type today) -- `forseti synth`'s own verdict on a *synthesized*
  precondition, one of `assumed_verified` / `discharged_verified` /
  `violated` / `vacuous` / `unknown` / `needs_contract` / `error`.

## Testing it yourself

`demo/render.py` has no test dependencies of its own. To sanity-check
changes:

```sh
# Replay against a hand-written events.jsonl:
python3 demo/render.py --replay /path/to/some/events.jsonl

# Live mode against a scratch project dir, appending lines by hand while it runs:
mkdir -p /tmp/demo-proj/.forseti
python3 demo/render.py /tmp/demo-proj &
echo '{"ts": 0, "type": "stop", "decision": "allow"}' >> /tmp/demo-proj/.forseti/events.jsonl
```
