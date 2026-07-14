"""Loop telemetry: a minimal, pluggable event seam (#29).

`run_loop` emits a structured `Event` at every transition through an injected
`EventSink`. The default `NullSink` makes emission a no-op, so the driver stays
deterministic and effect-free unless a sink is supplied. The canonical JSONL
*schema*, redaction, and replay tooling are W10/#15's job — this module ships
only the local event type and a few sinks (`JsonlSink` is the single
serialization boundary, swappable for #15's emitter later).
"""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TextIO


@dataclass(frozen=True)
class Event:
    """One loop event. Minimal and JSON-serializable (mirrors `to_dict` elsewhere).

    `seq` is a monotonic order stamp assigned by the driver. `type` is a plain
    string tag (no enum, to stay schema-light for #15). `index`/`k`/`verdict`
    are filled when the event is about a specific iteration; `detail` carries any
    extra key/values (e.g. a policy decision or a give-up reason).
    """

    seq: int
    type: str
    index: int | None = None
    k: int | None = None
    verdict: str | None = None
    detail: dict[str, str | int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable dict; every field is `int | str | None | dict`."""
        return asdict(self)


class EventSink(Protocol):
    """Receives the loop's events as they are emitted."""

    def emit(self, event: Event) -> None: ...


class NullSink:
    """The default sink: discards every event, keeping `run_loop` effect-free."""

    def emit(self, event: Event) -> None:
        pass


class EventEmitter:
    """Binds a monotonic `seq` counter to a sink — one home for the emit boilerplate.

    Every driver that emits (`run_loop`, `check_properties`) needs the same
    seq-and-sink pairing: a monotonic order stamp assigned to each `Event`, sent
    to the injected sink. Owning it here means a driver writes
    `emit = EventEmitter(sink).emit` instead of re-declaring the counter closure,
    and the `Event.seq` monotonicity guarantee lives in one tested place. A `None`
    sink defaults to `NullSink`, so an emitter with no sink is effect-free.
    """

    def __init__(self, sink: EventSink | None = None) -> None:
        self._sink = sink or NullSink()
        self._seq = itertools.count()

    def emit(self, type: str, **fields: Any) -> None:
        """Stamp the next `seq` onto an `Event(type, **fields)` and send it on."""
        self._sink.emit(Event(next(self._seq), type, **fields))


class ListSink:
    """Collects events in memory — for tests and to feed persistence/transcript."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class JsonlSink:
    """Writes each event as one JSON line to a text stream (the local sink #29 owns)."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, event: Event) -> None:
        self._stream.write(json.dumps(event.to_dict()) + "\n")
        self._stream.flush()


if TYPE_CHECKING:
    # mypy-only structural guards: fail type-checking if a concrete sink drifts
    # from the protocol (mirrors the fix.py guard pattern).
    def _null_is_sink(s: NullSink) -> EventSink:
        return s

    def _list_is_sink(s: ListSink) -> EventSink:
        return s

    def _jsonl_is_sink(s: JsonlSink) -> EventSink:
        return s
