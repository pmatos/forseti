"""In-package harness adapter implementations shipped with the `forseti` CLI.

Distinct from the `adapters/` directory at the repo root: that tree holds
per-harness human-facing docs, plugin manifests, and demo assets (RFC-0001).
Adapter *logic* that must ship with `pip install`/`uv tool install` (so a hook
invocation like `forseti claude-code-hook post-tool-use` works without a source
checkout on disk) lives here instead — see RFC-0004.
"""

from __future__ import annotations
