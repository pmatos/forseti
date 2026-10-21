"""Shared fail-closed error vocabulary for Claude Code gate operations."""

from __future__ import annotations


class UnitsUnavailable(RuntimeError):
    """A file's function definitions could not be enumerated.

    Distinct from "the file defines no functions" (an empty list): a failed
    enumeration must surface as a blocking `error` verdict, never be mistaken for
    a clean pass (kill-safety — a not-verified unit can't silently slip through).
    Raised when `forseti list-units` cannot be run or its payload is unusable, and
    when the gate's own configuration is (`FORSETI_BUILD_FLAGS`).
    """
