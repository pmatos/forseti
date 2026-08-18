"""Claude Code verify-gate adapter (RFC-0004): hooks + the `enable-project` installer.

`forseti_gate`/`event_log`/`session_start`/`post_tool_use`/`post_bash`/`stop_gate`
are the hook logic, dispatched in-process by `forseti claude-code-hook <name>`
(`core/cli.py`) so a hook entry needs only `forseti` on `PATH`, never a script
path on disk. `install.py` is the `forseti enable-project` merge/write logic
that generates and idempotently installs those hook entries into a target
project's Claude Code settings file.
"""

from __future__ import annotations
