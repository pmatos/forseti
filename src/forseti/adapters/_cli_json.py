"""Best-effort JSON transport for adapters that invoke the Forseti CLI."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_DIAGNOSTIC_LIMIT = 400


@dataclass(frozen=True)
class JsonCommandResult:
    """Decoded JSON or a diagnostic for a launch/decode failure."""

    payload: object | None
    failure: Literal["launch", "decode"] | None
    diagnostic: str


def _diagnostic(stderr: str, stdout: str) -> str:
    return (stderr or stdout).strip()[:_DIAGNOSTIC_LIMIT]


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
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostic = str(exc)
        return JsonCommandResult(
            payload=None,
            failure="launch",
            diagnostic=diagnostic,
        )

    diagnostic = _diagnostic(proc.stderr, proc.stdout)
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return JsonCommandResult(
            payload=None,
            failure="decode",
            diagnostic=diagnostic,
        )
    return JsonCommandResult(payload=payload, failure=None, diagnostic=diagnostic)
