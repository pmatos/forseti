"""Captured CLI commands and best-effort JSON transport for harness adapters.

Claude Code interprets process exceptions and exit status before decoding; Codex
and Oh My Pi want contained failures and decoded verdicts even on nonzero exits.
Both forms share capture, decoding, and diagnostic normalization here. Command
construction, payload validation, and harness failure policy stay with callers.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_DIAGNOSTIC_LIMIT = 400


@dataclass(frozen=True)
class JsonCommand:
    """One completed command, with decoding left to the caller's status policy."""

    returncode: int
    stdout: str
    stderr: str

    def decode(self) -> object:
        """Decode stdout, propagating JSON errors unchanged."""
        return json.loads(self.stdout)

    def diagnostic(self, *, limit: int = _DIAGNOSTIC_LIMIT) -> str:
        """Stripped, stderr-first output, bounded to the caller's diagnostic limit."""
        return (self.stderr or self.stdout).strip()[:limit]


def capture_json_command(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: str | Path | None = None,
) -> JsonCommand:
    """Capture a text command; propagate process failures without decoding.

    A completed command retains its status and raw output even when stdout is
    malformed JSON. Callers can reject a nonzero listing before decoding while
    accepting a nonzero verification's JSON verdict. Missing executables,
    timeouts, and other process failures keep their original exception objects.
    """
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )
    return JsonCommand(proc.returncode, proc.stdout, proc.stderr)


@dataclass(frozen=True)
class JsonCommandResult:
    """Decoded JSON or a diagnostic for a launch/decode failure."""

    payload: object | None
    failure: Literal["launch", "decode"] | None
    diagnostic: str


def run_json(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: str | Path | None = None,
) -> JsonCommandResult:
    """Run a CLI command and decode stdout without interpreting its payload.

    Nonzero exit status is not itself a failure: Forseti commands can return a
    meaningful JSON verdict alongside a nonzero status. Callers retain command
    construction, payload validation, and harness-specific failure policy.
    """
    try:
        command = capture_json_command(argv, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostic = str(exc)
        return JsonCommandResult(
            payload=None,
            failure="launch",
            diagnostic=diagnostic,
        )

    diagnostic = command.diagnostic()
    try:
        payload = command.decode()
    except ValueError:
        return JsonCommandResult(
            payload=None,
            failure="decode",
            diagnostic=diagnostic,
        )
    return JsonCommandResult(payload=payload, failure=None, diagnostic=diagnostic)
