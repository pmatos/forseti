"""Put the adapter's `tools/` on sys.path for the remaining adapter-local tests.

The hook logic itself now ships inside the `forseti` package
(`src/forseti/adapters/claude_code/`, RFC-0004) and is covered under the
canonical `tests/adapters/claude_code/` suite. `tools/trace_to_mermaid.py` is a
standalone dev tool that stays outside the package, so its test still imports
it directly by adding `tools/` to sys.path.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "tools"))
