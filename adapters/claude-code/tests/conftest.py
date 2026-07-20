"""Put the adapter's `hooks/` and `tools/` on sys.path for the adapter tests.

The Claude Code adapter is a self-contained plugin outside the `forseti` package
(the root `pytest`/`mypy`/`ruff` run on `src tests` only), so its modules are not
importable as a package. These tests import them directly by adding the two
script directories to sys.path.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _sub in ("hooks", "tools"):
    sys.path.insert(0, os.path.join(_HERE, "..", _sub))
