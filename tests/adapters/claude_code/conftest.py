from __future__ import annotations

from collections.abc import Iterator

import pytest

from forseti.adapters.claude_code import forseti_gate as gate


@pytest.fixture(autouse=True)
def _reset_env_config_errors() -> Iterator[None]:
    """`gate._ENV_CONFIG_ERRORS` is a module-level list every `env_int`/`env_float`
    call appends to (issue #95 review) -- safe in production (each hook is its
    own short-lived process), but shared across this whole long-lived test
    session otherwise: a test that intentionally triggers a malformed env var
    would otherwise leak a recorded error into every later test's `stop_gate`
    guard, well outside the file that caused it.
    """
    saved = list(gate._ENV_CONFIG_ERRORS)
    gate._ENV_CONFIG_ERRORS.clear()
    yield
    gate._ENV_CONFIG_ERRORS.clear()
    gate._ENV_CONFIG_ERRORS.extend(saved)
