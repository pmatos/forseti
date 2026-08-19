from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_update_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep ordinary CLI tests offline and out of the user's real cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "update-cache"))

    def offline(_request: object, *, timeout: float) -> None:
        raise urllib.error.URLError("offline test default")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
