"""The `cli.command` trace seam: one event per invocation, every exit path (#301).

:data:`forseti.core.events.CLI_COMMAND` *states* the policy -- one event per
invocation of a traced subcommand, on every exit path, emitted by the CLI
handler and never by the engine. This module is the one place that policy is
*carried out*. Its three call sites (`synth`, `discharge`, `semantic-loop`)
differ only in the command's name and in the two or three fields that command
adds to the common row published at `docs/design/0001-harness-portability.md`.

It lives here rather than in either caller: `_precond_cli` is precond-only glue,
so a general CLI-tracing concern does not belong in it, and putting it in `cli`
would make `_precond_cli` import its own importer. This module depends on
`.events` and the stdlib only, so it sits strictly below both.

The clock and the sink stay inside. Neither is a real seam: there is one
monotonic clock and one sink, and a fake sink would actively *weaken* the tests
-- the published contract is a JSON line, so asserting a Python dict would stop
exercising ``sort_keys``, the single-line append and JSON-serialisability. The
variation that is real is already a parameter: ``store_root``, threaded from
``--store-root`` through ``args``, which is what gives every test its isolation.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from typing import Any

from .events import CLI_COMMAND, record_event

COMMON_FIELDS = frozenset({"command", "source", "function", "exit_code", "duration_s"})
"""The half of the published row `traced` fills for every command.

A per-command `fields` builder must return none of these -- see `traced`.
"""


def traced[ResultT](
    command: str,
    run: Callable[[argparse.Namespace], tuple[int, ResultT]],
    args: argparse.Namespace,
    *,
    fields: Callable[[argparse.Namespace, ResultT], dict[str, Any]],
) -> int:
    """Run `run(args)`, record one `cli.command` event for it, return its code.

    **Totality is a precondition on `run`, not something this enforces.** Every
    return path of `run` -- an early argument error, a caught engine error,
    `--emit-only`, a verdict -- lands here because `run` *returns*
    `(exit_code, result)` on all of them rather than calling `sys.exit`. There
    is deliberately no `try`/`finally`: an escaping exception produces no event
    today, and inventing one would change a published wire format.

    Ordering: the clock is read before `run`, and again before the record call,
    so `duration_s` spans the handler alone and never the write. The event is
    appended after `run` returns, so a command's stdout always precedes its
    trace line.

    `fields(args, result)` supplies the command's own half of the row. It must
    return **no key in** :data:`COMMON_FIELDS`: a collision is a `TypeError`
    raised while building the call, which escapes `record_event`'s swallow-all.
    That is a static property of the module-level builders, so it cannot fire in
    production without failing the suite first. The loud form is chosen over a
    merged dict literal, which would silently overwrite a common field instead.

    `record_event` serialises with `sort_keys=True`, so key *order* is not
    load-bearing: the wire format is pinned by the key set and the values alone.
    """
    started = time.monotonic()
    exit_code, result = run(args)
    record_event(
        args.store_root,
        CLI_COMMAND,
        command=command,
        source=str(args.source),
        function=args.function,
        **fields(args, result),
        exit_code=exit_code,
        duration_s=time.monotonic() - started,
    )
    return exit_code
