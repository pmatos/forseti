#!/usr/bin/env python3
"""Serve the observability canvas: `index.html` plus a polling `.forseti/events.jsonl`
feed, for the write -> verify -> counterexample -> fix demo's "right pane".

This is demo/e2e-test tooling, not part of the `forseti` package -- stdlib-only
(no pip deps), mirroring `demo/render.py`'s own dependency-free philosophy and
its two modes:

- Live (default): `server.py <project_dir>` tails
  `<project_dir>/.forseti/events.jsonl` as it grows.
- Replay: `server.py --replay <trace-file> [--speed N]` serves the same page
  fed from a previously captured trace, paced by recorded timestamp deltas
  (capped, like `render.py`'s own `_MAX_REPLAY_DELAY_S`) instead of a live
  tail. Rehearsal/fallback mode for when a live demo run doesn't cooperate.

Live-update mechanism: plain polling (`GET /events?since=<cursor>`), not
Server-Sent Events. A canvas redrawn every ~0.7s has no need for sub-second
push, and polling keeps both modes -- live tail and timestamp-paced replay --
behind one stateless, GET-only endpoint with no held-open connections, no
pusher thread, and no `BrokenPipeError` handling to get wrong mid-demo.

The `cursor` the client gets back and echoes on the next request is opaque to
it and means something different per mode: a byte offset into
`events.jsonl` for the live tail, an event-index for replay. This lets both
modes share one `EventSource.poll(since) -> dict` contract and one page/JS.

Event schema consumed here is the same one `demo/render.py` already parses;
see its module docstring for the three interleaved event families
(`src/forseti/adapters/claude_code/event_log.py`,
`src/forseti/core/events.py`, and this demo's own `demo/bin/forseti` `cli`
events). This server does not interpret event fields itself -- it only
tails/paces/serves raw JSON lines; `index.html`'s JS does the vocabulary-aware
rendering (kept in sync with `render.py`'s color/label policy by hand).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_CANVAS_DIR = Path(__file__).resolve().parent
_INDEX_FILE = _CANVAS_DIR / "index.html"
_MAX_REPLAY_DELAY_S = 3.0  # matches demo/render.py's own per-gap cap
_DEFAULT_PORT = 8765


def _parse_line(raw: bytes) -> dict[str, Any] | None:
    line = raw.strip()
    if not line:
        return None
    try:
        ev = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return ev if isinstance(ev, dict) else None


class EventSource:
    """`poll(since)` -> `{"events", "cursor", "reset", "waiting"}`; `describe()` -> `dict`."""  # noqa: E501

    def poll(self, since: int) -> dict[str, Any]:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        raise NotImplementedError


class LiveTailSource(EventSource):
    """Tails `<project_dir>/.forseti/events.jsonl`; cursor is a byte offset.

    Tolerates the file not existing yet (`waiting: true`, cursor stays 0),
    in-place truncation and inode rotation -- a fresh demo run reusing the
    same project dir (`reset: true`, served from byte 0), and a torn
    trailing write still in flight from a concurrent appender: bytes after
    the last complete `\\n` are held back and the returned cursor never
    advances past them, so the next poll (after the writer's next flush)
    picks the now-completed line up. Binary reads throughout, since a
    counterexample can carry arbitrary bytes that a text-mode/character
    offset would desync on.
    """

    def __init__(self, project_dir: Path) -> None:
        self._path = project_dir / ".forseti" / "events.jsonl"
        self._project_dir = project_dir
        self._lock = threading.Lock()
        self._known_inode: int | None = None
        self._known_size = 0

    def describe(self) -> dict[str, Any]:
        return {"mode": "live", "target": str(self._path)}

    def poll(self, since: int) -> dict[str, Any]:
        with self._lock:
            try:
                st = self._path.stat()
            except OSError:
                self._known_inode = None
                self._known_size = 0
                return {
                    "events": [],
                    "cursor": 0,
                    "reset": since != 0,
                    "waiting": True,
                }

            rotated = self._known_inode is not None and st.st_ino != self._known_inode
            truncated = st.st_size < self._known_size
            reset = rotated or truncated or since > st.st_size
            self._known_inode = st.st_ino
            self._known_size = st.st_size

            start = 0 if reset else since

            try:
                with open(self._path, "rb") as fh:
                    fh.seek(start)
                    chunk = fh.read()
            except OSError:
                return {"events": [], "cursor": start, "reset": reset, "waiting": True}

            last_nl = chunk.rfind(b"\n")
            if last_nl == -1:
                # No complete line since `start` yet -- don't advance the
                # cursor past a partial trailing write.
                return {"events": [], "cursor": start, "reset": reset, "waiting": False}

            complete = chunk[: last_nl + 1]
            new_cursor = start + last_nl + 1
            events = []
            for raw_line in complete.split(b"\n"):
                ev = _parse_line(raw_line)
                if ev is not None:
                    events.append(ev)
            return {
                "events": events,
                "cursor": new_cursor,
                "reset": reset,
                "waiting": False,
            }


class ReplaySource(EventSource):
    """Replays a saved trace; cursor is an event index into it.

    Release offsets (seconds from replay start) are precomputed once, at
    construction, exactly mirroring `demo/render.py::run_replay`'s pacing:
    the delay before each event is its recorded `ts` delta from the
    previous one, divided by `speed`, capped per-gap at
    `_MAX_REPLAY_DELAY_S` -- with `speed` omitted or falsy, every event's
    offset is 0 (instant). Each poll then just compares wall-clock elapsed
    time against that fixed table -- no background thread, no timer drift,
    and trivially safe under concurrent requests from multiple tabs.
    """

    def __init__(self, trace_file: Path, speed: float | None) -> None:
        try:
            text = trace_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"canvas/server.py: cannot read {trace_file}: {exc}", file=sys.stderr)
            sys.exit(1)

        events: list[dict[str, Any]] = []
        for line in text.splitlines():
            ev = _parse_line(line.encode("utf-8"))
            if ev is not None:
                events.append(ev)

        offsets: list[float] = []
        cumulative = 0.0
        prev_ts: float | None = None
        for ev in events:
            ts = ev.get("ts")
            if (
                speed
                and speed > 0
                and prev_ts is not None
                and isinstance(ts, (int, float))
            ):
                delta = ts - prev_ts
                if delta > 0:
                    cumulative += min(_MAX_REPLAY_DELAY_S, delta / speed)
            offsets.append(cumulative)
            if isinstance(ts, (int, float)):
                prev_ts = ts

        self._trace_file = trace_file
        self._speed = speed
        self._events = events
        self._offsets = offsets
        self._start = time.monotonic()

    def describe(self) -> dict[str, Any]:
        pace = f"speed={self._speed}" if self._speed else "instant"
        return {
            "mode": "replay",
            "target": str(self._trace_file),
            "pace": pace,
            "n_events": len(self._events),
        }

    def poll(self, since: int) -> dict[str, Any]:
        elapsed = time.monotonic() - self._start
        due = 0
        for offset in self._offsets:
            if offset <= elapsed:
                due += 1
            else:
                break
        since = max(0, min(since, due))
        return {
            "events": self._events[since:due],
            "cursor": due,
            "reset": False,
            "waiting": False,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "ForsetiCanvas/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # keep the terminal quiet during a live demo

    def _send_json(self, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        # An explicit whitelist of the handful of paths this server serves,
        # keyed off this file's own directory -- not `SimpleHTTPRequestHandler`
        # + cwd, so nothing outside `demo/canvas/` is ever reachable.
        if parsed.path in ("/", "/index.html"):
            self._send_file(_INDEX_FILE, "text/html; charset=utf-8")
            return
        if parsed.path == "/events":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                since = int(qs.get("since", ["0"])[0])
            except ValueError:
                since = 0
            since = max(0, since)
            source: EventSource = self.server.event_source  # type: ignore[attr-defined]
            self._send_json(source.poll(since))
            return
        if parsed.path == "/meta":
            source = self.server.event_source  # type: ignore[attr-defined]
            self._send_json(source.describe())
            return
        self.send_error(404)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="server.py",
        description=(
            "Serve the Forseti demo's observability canvas: index.html plus a "
            "polling feed over a project's .forseti/events.jsonl."
        ),
    )
    p.add_argument(
        "project_dir",
        nargs="?",
        help="project directory containing .forseti/events.jsonl (live mode)",
    )
    p.add_argument(
        "--replay",
        metavar="TRACE_FILE",
        help="replay a saved events.jsonl file instead of tailing a live project",
    )
    p.add_argument(
        "--speed",
        type=float,
        default=None,
        help=(
            "pace --replay using recorded timestamp deltas divided by SPEED "
            "(e.g. 1 = real time, 4 = 4x fast-forward, capped per-gap); "
            "omit for instant replay"
        ),
    )
    p.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"TCP port to listen on (default {_DEFAULT_PORT})",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.replay:
        if args.project_dir:
            print(
                "canvas/server.py: pass either a project_dir or --replay, not both",
                file=sys.stderr,
            )
            return 2
        source: EventSource = ReplaySource(Path(args.replay), speed=args.speed)
    else:
        if args.speed is not None:
            print("canvas/server.py: --speed only applies to --replay", file=sys.stderr)
            return 2
        if not args.project_dir:
            print(
                "canvas/server.py: a project_dir is required in live mode "
                "(or use --replay)",
                file=sys.stderr,
            )
            return 2
        source = LiveTailSource(Path(args.project_dir))

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.event_source = source  # type: ignore[attr-defined]
    desc = source.describe()
    print(f"forseti canvas -- {desc} -- http://127.0.0.1:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
