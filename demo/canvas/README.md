# demo/canvas -- live observability canvas

A browser-based, diagrammatic view of the write -> verify -> counterexample ->
fix loop, meant to run in a browser window next to `demo/render.py`'s
terminal pane and a real `claude` session -- not a replacement for either.

It renders a small fixed diagram of the four actors in the loop (**Agent**,
**forseti**, **ESBMC**, **LLM**), pulses/labels the edge between them as
events arrive, and keeps a full, scrollable, rewindable text log of the same
event stream underneath.

Stdlib-only on the server side (`http.server`/`socketserver` via
`ThreadingHTTPServer`, no pip deps) and a single self-contained
`index.html` on the client side (plain JS + inline SVG, no build step, no
CDN dependencies) -- both run fully offline.

## Live mode

In one terminal:

```sh
source demo/env.sh
demo/scaffold/init.sh /tmp/my-demo-workspace   # once, to scaffold a workspace
python3 demo/canvas/server.py /tmp/my-demo-workspace
```

Open `http://localhost:8765` in a browser (pass `--port N` to use a
different port).

In a second terminal, next to it:

```sh
source demo/env.sh
cd /tmp/my-demo-workspace
claude
```

(Optionally run `demo/render.py /tmp/my-demo-workspace` in a third pane for
the terminal sequence-diagram view -- both tools tail the same
`.forseti/events.jsonl` independently and don't interfere with each other.)

The page polls `GET /events?since=<cursor>` every ~700ms. It tolerates the
session not having started yet (shows "waiting for session…" until
`.forseti/events.jsonl` appears) and a fresh demo run reusing the same
workspace (detects the file's truncation/rotation and clears the page,
replaying from the start of the new trace).

## Replay mode

For rehearsal, or when a live demo run isn't available:

```sh
python3 demo/canvas/server.py --replay /path/to/some/events.jsonl
python3 demo/canvas/server.py --replay /path/to/some/events.jsonl --speed 1   # real-time pacing
python3 demo/canvas/server.py --replay /path/to/some/events.jsonl --speed 4   # 4x fast-forward
```

Serves the same `index.html`, fed from the recorded trace instead of a live
tail. Without `--speed`, every event is available immediately (instant
replay, matching `demo/render.py --replay`'s own default). With `--speed N`,
events become available paced by their recorded timestamp deltas divided by
`N`, capped per-gap the same way `render.py` caps its own replay (a long
real pause in the original session doesn't stall the page for minutes).

`--replay` and a `project_dir` are mutually exclusive, and `--speed` only
applies alongside `--replay` -- the same CLI contract as `demo/render.py`.

## Live-update mechanism

Plain polling, not Server-Sent Events. A canvas redrawn a couple of times a
second has no real need for sub-second push, and polling lets one stateless,
GET-only `EventSource.poll(since)` contract serve both modes -- tailing a
live, concurrently-appended file, and pacing a fixed replay off wall-clock
elapsed time -- with no held-open connections, no pusher thread, and no
`BrokenPipeError`/disconnect handling to get wrong in the middle of a live
demo. `server.py`'s module docstring has the fuller rationale.

## What gets rendered

- The diagram: an edge pulse (a small moving dot, plus a short-lived label
  badge near the edge) each time two actors exchange something. Unresolved
  outcomes (`UNKNOWN` / `NEEDS_CONTRACT` / `ERROR` / `skipped`) render as a
  diamond instead of a circle, on a dashed badge, in addition to their color
  -- distinct at a glance, never styled as a pass.
- The log: one line per event, oldest at the top, newest at the bottom,
  never capped or truncated -- the full session's events stay in the page so
  scrolling up rewinds through history. Auto-scroll only kicks in while
  already pinned to the bottom, so scrolling up to read back doesn't get
  yanked back down by the next poll.

Event-type-to-edge mapping (see `index.html`'s `CHOREOGRAPHY` table for the
exact choreography):

- `edit` -- a pulse on the Agent node itself (Claude wrote/edited a file);
  not really a cross-actor exchange.
- `verify` / `gate` / `gate.decision` / `stop` -- the built-in safety-gate
  path: Agent <-> forseti <-> ESBMC.
- `cli` events (the `demo/bin/forseti` shim's trace of `forseti synth` /
  `discharge` / etc invocations) -- the deterministic precondition path,
  also Agent <-> forseti <-> ESBMC.
- `property.proposed` / `property.check.start` / `property.verdict` --
  the semantic-property path: forseti <-> LLM, forseti <-> ESBMC.

## Vocabulary discipline (hard rule, see this repo's root CLAUDE.md)

Same discipline as `demo/render.py`, mirrored by hand in `index.html`'s JS:
ESBMC never "proves" or "corrects" anything -- it returns a bounded verdict.
This page never renders "proven" or "correct"; it always renders
VERIFIED/VIOLATED/UNKNOWN (or the `forseti synth`/`discharge` assessment
vocabulary: `assumed_verified`/`discharged_verified`/`violated`/`vacuous`/
`unknown`/`needs_contract`/`error`), and UNKNOWN/NEEDS_CONTRACT/ERROR always
get the same unresolved (yellow + dashed) styling -- never colored as a pass.

Two different "needs_contract" claims stay distinct, exactly as in
`render.py`: a `verify` event's `needs_contract` **verdict** (the safety gate
couldn't check a pointer/array-taking function without a synthesized
harness -- non-blocking, *not* evidence of safety) versus a `cli` event's own
`assessment` field (parsed out of `output_tail` with the same best-effort
regex `render.py` uses, since `forseti synth`/`discharge` have no dedicated
event type) -- `forseti synth`'s own verdict on a *synthesized* precondition.
They render with separate color maps and separate on-screen labels, never
merged into one lookup.

## Testing it yourself

No test dependencies of its own (this is demo tooling, not covered by
`ruff check src tests` / `pytest`'s `testpaths=["tests"]`). To sanity-check
changes:

```sh
# Replay against a hand-written events.jsonl:
python3 demo/canvas/server.py --replay /path/to/some/events.jsonl &
curl -s http://localhost:8765/meta
curl -s 'http://localhost:8765/events?since=0'

# Live mode against a scratch project dir, appending lines by hand while it runs:
mkdir -p /tmp/canvas-scratch/.forseti
python3 demo/canvas/server.py /tmp/canvas-scratch &
curl -s 'http://localhost:8765/events?since=0'    # {"events": [], ..., "waiting": true}
echo '{"ts": 0, "type": "stop", "decision": "allow"}' >> /tmp/canvas-scratch/.forseti/events.jsonl
curl -s 'http://localhost:8765/events?since=0'    # now returns the appended event
```

Grep for vocabulary-discipline regressions:

```sh
grep -niE '\b(proven|proof|correct)\b' demo/canvas/index.html   # must be empty
```
