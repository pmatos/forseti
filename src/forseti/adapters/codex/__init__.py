"""Codex verify-gate adapter (#212): the `PostToolUse` hook + `enable-project` install.

`verify_hook.py` is the `PostToolUse` gate logic, dispatched in-process by
`forseti codex-hook verify` (`core/cli.py`) so the wired command needs only
`forseti` on `PATH`, never a script path on disk. `install.py` is the
`forseti enable-project --harness codex` merge/write logic that generates and
idempotently installs that hook entry into a target project's
`.codex/config.toml`.
"""

from __future__ import annotations
