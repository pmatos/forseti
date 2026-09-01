"""Harness auto-detection for `forseti enable-project`/`disable-project` (#212).

Detection is best-effort and must fail closed: `detect_harness()` returns
`None` -- never a guess -- when the session environment is ambiguous (more
than one harness's markers present) or unknown (none present), so callers
require an explicit `--harness` instead of silently installing the wrong one.

Markers are the *exact* env var names each harness's own CLI sets, not a
prefix match: e.g. a `CODEX_COMPANION_SESSION_ID` set by an unrelated
in-session tool must not be mistaken for genuine Codex CLI session env
(`CODEX_SESSION_ID` / `CODEX_THREAD_ID`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum

_CODEX_ENV_VARS: tuple[str, ...] = ("CODEX_SESSION_ID", "CODEX_THREAD_ID")
_CLAUDE_CODE_ENV_VAR = "CLAUDECODE"


class Harness(StrEnum):
    """A harness `enable-project`/`disable-project` knows how to target."""

    CLAUDE_CODE = "claude-code"
    CODEX = "codex"


def detect_harness(env: Mapping[str, str] | None = None) -> Harness | None:
    """Best-effort harness from unambiguous session env vars, else `None`.

    `env` defaults to `os.environ`; tests pass an explicit mapping instead of
    relying on ambient process state.
    """
    active_env = os.environ if env is None else env
    is_codex = any(active_env.get(name) for name in _CODEX_ENV_VARS)
    is_claude_code = bool(active_env.get(_CLAUDE_CODE_ENV_VAR))
    if is_codex and is_claude_code:
        return None
    if is_codex:
        return Harness.CODEX
    if is_claude_code:
        return Harness.CLAUDE_CODE
    return None
