"""Oh My Pi adapter (#249): the native `tool_result` gate + `enable-project` install.

`verify_hook.py` is the gate logic, dispatched in-process by `forseti omp-hook
tool-result` (`core/cli.py`) so the packaged extension needs only `forseti` on
`PATH`, never a script path on disk. `install.py` is the `forseti
enable-project --harness oh-my-pi` merge/write logic that generates and
idempotently installs the extension and `.omp/mcp.json` entry into a target
project.
"""

from __future__ import annotations
