"""Behavioural tests for `verify` that need no esbmc binary.

The subprocess boundary is exercised through error paths (a missing binary, a
timeout) so these stay fast and deterministic. Real-verdict end-to-end tests
live in test_verify_integration.py and require esbmc.
"""

import subprocess
from pathlib import Path

import pytest

from forseti.esbmc.result import Error, Unknown, UnknownReason
from forseti.esbmc import runner
from forseti.esbmc.runner import verify


def test_missing_binary_returns_error(tmp_path: Path) -> None:
    src = tmp_path / "x.c"
    src.write_text("int main(void){ return 0; }\n")
    result = verify(src, unwind=4, esbmc_bin="forseti-no-such-esbmc-binary")
    assert isinstance(result, Error)
    assert "not found" in result.message.lower()
    # provenance is recorded even when the binary never ran
    assert "--unwind" in result.meta.argv
    assert "4" in result.meta.argv
    assert "--no-unwinding-assertions" in result.meta.argv


def test_non_executable_binary_returns_error(tmp_path: Path) -> None:
    # A bad-but-existing esbmc_bin (here: a directory) makes subprocess.run
    # raise an OSError other than FileNotFoundError; the wrapper must return a
    # typed Error with provenance, never leak the exception.
    src = tmp_path / "x.c"
    src.write_text("int main(void){ return 0; }\n")
    bad_bin = tmp_path / "not_a_binary_dir"
    bad_bin.mkdir()
    result = verify(src, unwind=4, esbmc_bin=str(bad_bin))
    assert isinstance(result, Error)
    assert result.meta.exit_code == -1
    assert "--unwind" in result.meta.argv


def test_subprocess_timeout_is_unknown_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "x.c"
    src.write_text("int main(void){ return 0; }\n")

    def boom(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(
            cmd="esbmc", timeout=1.0, output=b"partial output\n"
        )

    monkeypatch.setattr(runner.subprocess, "run", boom)
    result = verify(src, unwind=4, timeout_s=1.0)
    assert isinstance(result, Unknown)
    assert result.reason is UnknownReason.TIMEOUT
    # partial output captured on the result, decoded to text
    assert result.meta.stdout == "partial output\n"
