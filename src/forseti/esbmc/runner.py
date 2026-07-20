"""Invoke ESBMC and classify its output into a typed `EsbmcResult`."""

from __future__ import annotations

import math
import re
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from .cex_parser import Frontend, parse_counterexample
from .result import (
    Error,
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)

_FAILED = "VERIFICATION FAILED"
_SUCCESSFUL = "VERIFICATION SUCCESSFUL"
_CEX_START = "[Counterexample]"


def _has_banner(text: str, banner: str) -> bool:
    """True only if `banner` appears as a standalone output line.

    ESBMC prints its verdict on its own line. Requiring a line match keeps a
    broken invocation that merely *echoes* the banner text (a frontend
    diagnostic quoting source, a preprocessor error) from being read as a
    verdict, per the "never silently pass" invariant.
    """
    return any(line.strip() == banner for line in text.splitlines())


def _counterexample(text: str) -> str:
    """The trace text spanning `[Counterexample]` up to the terminal FAILED banner.

    Slices at the *last* standalone FAILED line, not the first substring match —
    so a `VERIFICATION FAILED` echoed inside the `Violated property:` block (e.g.
    an `__ESBMC_assert` message) doesn't truncate the trace early. Callers only
    invoke this once a standalone FAILED banner is known to exist.
    """
    lines = text.splitlines()
    cut = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == _FAILED:
            cut = i
            break
    body = "\n".join(lines[:cut])
    start = body.find(_CEX_START)
    return (body[start:] if start != -1 else body).strip()


def _error_message(text: str) -> str:
    """First `ERROR:`-prefixed line, so a known failure is self-describing."""
    for line in text.splitlines():
        if line.startswith("ERROR:"):
            return line[len("ERROR:") :].strip()
    return "esbmc reported an error"


def classify(meta: RunMeta, frontend: Frontend = Frontend.C) -> EsbmcResult:
    """Map one finished ESBMC run to a verdict.

    Classification keys on stdout/stderr markers, never the exit code (a
    timeout and a real violation both exit 1). Known invocation errors are
    checked before any verdict banner, and the SUCCESSFUL/FAILED banners are
    matched only as standalone lines — so a broken invocation that merely
    echoes banner text (a parse diagnostic quoting source) is never read as a
    verdict. Unrecognised output becomes `Error`, never a verdict. On VIOLATED
    the raw trace is parsed into a typed `Counterexample` for `frontend`;
    parsing returns `None` rather than disturbing the verdict on failure.
    """
    text = meta.stdout + "\n" + meta.stderr
    if (
        meta.exit_code == 6
        or "PARSING ERROR" in text
        or "failed to open input file" in text
    ):
        return Error(meta, _error_message(text))
    if _has_banner(text, _FAILED):
        raw = _counterexample(text)
        return Violated(meta, raw, parse_counterexample(raw, frontend))
    if _has_banner(text, _SUCCESSFUL):
        return Verified(meta)
    if "VERIFICATION UNKNOWN" in text:
        return Unknown(meta, UnknownReason.UNCLASSIFIED)
    if "Timed out" in text:
        return Unknown(meta, UnknownReason.TIMEOUT)
    if "Out of memory" in text:
        return Unknown(meta, UnknownReason.MEMOUT)
    return Error(meta, "unclassified output")


# Wall-clock slack added on top of esbmc's own --timeout before we hard-kill it.
_GRACE_S = 5.0
_VERSION_RE = re.compile(r"ESBMC version (\S+)")


def _parse_version(text: str) -> str:
    match = _VERSION_RE.search(text)
    return match.group(1) if match else ""


def _as_text(value: str | bytes | None) -> str:
    """Captured subprocess output as text (decode bytes, treat None as empty)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _run_meta(
    argv: tuple[str, ...],
    *,
    exit_code: int,
    duration_s: float,
    stdout: str,
    stderr: str,
) -> RunMeta:
    """Assemble the provenance of one finished-or-failed esbmc invocation.

    The single place that turns a subprocess outcome into a `RunMeta`, so the
    version banner is parsed from the captured output the same way on every
    exit path. esbmc prints its banner before it starts solving, so a run that
    times out *after* the banner still records the build here — provenance the
    success-only version parse used to drop. When no banner was captured (the
    binary never ran, or the timeout fired first) `esbmc_version` stays empty.
    """
    return RunMeta(
        esbmc_version=_parse_version(stdout) or _parse_version(stderr),
        argv=argv,
        exit_code=exit_code,
        duration_s=duration_s,
        stdout=stdout,
        stderr=stderr,
    )


def _encode_timeout_s(timeout_s: float) -> str:
    """esbmc's `--timeout` value for a budget of `timeout_s` seconds.

    esbmc takes a whole-second count with a unit suffix. We round *up* to whole
    seconds and never below `1s`: a positive budget must stay a positive,
    bounded timeout. Plain truncation is the trap here — `int(0.5)` is `0`, and
    esbmc reads `--timeout 0s` as *no* timeout, silently turning a sub-second
    budget into an unbounded run. Rounding up also keeps esbmc's own timeout
    strictly under the wall-clock backstop (`timeout_s + _GRACE_S`), so esbmc
    still self-terminates with a clean `Timed out` before we hard-kill it.
    """
    return f"{max(1, math.ceil(timeout_s))}s"


def build_argv(
    source: Path,
    *,
    unwind: int,
    function: str | None = None,
    timeout_s: float | None = None,
    extra_flags: Sequence[str] = (),
    esbmc_bin: str = "esbmc",
) -> tuple[str, ...]:
    """The exact esbmc command line for these verification parameters.

    The single home for every argv decision: the recommended
    `--unwind N --no-unwinding-assertions` bound, the optional `--function`
    entry point, the verbatim `extra_flags` passthrough, and the `--timeout`
    encoding. Keeping it pure and separate from the subprocess call makes the
    command line directly testable without invoking esbmc, and gives callers one
    place to spell a flag rather than open-coding `["--function", name]` at each
    site. `function` and `--timeout` come *after* `extra_flags` so an explicit
    passthrough flag keeps its position relative to esbmc's option precedence.
    """
    argv = [
        esbmc_bin,
        str(source),
        "--unwind",
        str(unwind),
        "--no-unwinding-assertions",
        *extra_flags,
    ]
    if function is not None:
        argv += ["--function", function]
    if timeout_s is not None:
        argv += ["--timeout", _encode_timeout_s(timeout_s)]
    return tuple(argv)


def verify(
    source: Path,
    *,
    unwind: int,
    timeout_s: float | None = None,
    function: str | None = None,
    extra_flags: Sequence[str] = (),
    esbmc_bin: str = "esbmc",
    frontend: Frontend = Frontend.C,
) -> EsbmcResult:
    """Run ESBMC on a `source` and return the typed verdict.

    Uses the recommended `--unwind N --no-unwinding-assertions` bound. `unwind`
    is required and recorded in the result's `argv`, so a VERIFIED stays
    qualified as "verified up to k". `function` selects the entry point to
    verify (esbmc's `--function`), spelled as data here rather than by the
    caller. When `timeout_s` is set, esbmc's own `--timeout` is used (it yields a
    clean `Timed out`), with a wall-clock backstop that maps a Python-level
    timeout to `Unknown(TIMEOUT)`. `frontend` selects the counterexample parser
    (C only, for now).
    """
    argv = build_argv(
        source,
        unwind=unwind,
        function=function,
        timeout_s=timeout_s,
        extra_flags=extra_flags,
        esbmc_bin=esbmc_bin,
    )
    proc_timeout = timeout_s + _GRACE_S if timeout_s is not None else None

    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=proc_timeout
        )
    except OSError as exc:
        # The binary never ran (missing, non-executable, a directory, ...);
        # record the intended argv for provenance. FileNotFoundError is an
        # OSError subclass, so a missing binary keeps its specific message;
        # PermissionError and other OSErrors become a typed Error too, never a
        # leaked exception, per the wrapper's "invocation failures are Error"
        # invariant.
        meta = _run_meta(
            argv,
            exit_code=-1,
            duration_s=time.monotonic() - start,
            stdout="",
            stderr="",
        )
        message = (
            f"esbmc binary not found: {esbmc_bin}"
            if isinstance(exc, FileNotFoundError)
            else f"esbmc invocation failed: {esbmc_bin}: {exc}"
        )
        return Error(meta, message)
    except subprocess.TimeoutExpired as exc:
        meta = _run_meta(
            argv,
            exit_code=-1,
            duration_s=time.monotonic() - start,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
        )
        return Unknown(meta, UnknownReason.TIMEOUT)

    meta = _run_meta(
        argv,
        exit_code=proc.returncode,
        duration_s=time.monotonic() - start,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
    return classify(meta, frontend)
