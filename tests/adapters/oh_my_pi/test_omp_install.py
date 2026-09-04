"""Tests for `forseti enable-project --harness oh-my-pi`'s install logic (#249).

Hermetic: `merge_mcp_config`/`_strip_mcp_server` take/return plain dicts, no
I/O. `install`/`remove` are exercised against `tmp_path`, never a real project.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forseti.adapters.oh_my_pi import install as install_module
from forseti.adapters.oh_my_pi.install import (
    InstallOutcome,
    ProjectConfigError,
    RemoveOutcome,
    install,
    merge_mcp_config,
    remove,
)

_CONFIG_PATH = Path("mcp.json")


def test_extension_source_matches_reference_file() -> None:
    # `install.py`'s embedded `_EXTENSION_SOURCE` is a hand-kept copy of the
    # repo-root reference file (its own module docstring says so); this pins
    # the two together so one can drift from the other only if this test is
    # also updated.
    reference_path = (
        Path(__file__).resolve().parents[3]
        / "adapters"
        / "oh-my-pi"
        / "forseti-gate.ts"
    )
    assert reference_path.read_text() == install_module._EXTENSION_SOURCE


def test_extension_source_starts_and_ends_with_sentinel() -> None:
    assert install_module._EXTENSION_SOURCE.startswith(install_module._MARKER_START)
    assert install_module._EXTENSION_SOURCE.rstrip("\n").endswith(
        install_module._MARKER_END
    )


# --- merge_mcp_config / _strip_mcp_server -----------------------------------


def test_merge_mcp_config_on_empty_adds_forseti_server() -> None:
    merged = merge_mcp_config({}, _CONFIG_PATH)
    assert merged["mcpServers"]["forseti"] == {"command": "forseti", "args": ["mcp"]}


def test_merge_mcp_config_preserves_unrelated_keys_and_servers() -> None:
    existing = {
        "$schema": "https://example.com/schema.json",
        "disabledServers": ["other"],
        "mcpServers": {
            "other": {"command": "npx", "args": ["-y", "other-server"]},
        },
    }
    merged = merge_mcp_config(existing, _CONFIG_PATH)
    assert merged["$schema"] == existing["$schema"]
    assert merged["disabledServers"] == ["other"]
    assert merged["mcpServers"]["other"] == existing["mcpServers"]["other"]
    assert merged["mcpServers"]["forseti"] == {"command": "forseti", "args": ["mcp"]}


def test_merge_mcp_config_is_idempotent() -> None:
    first = merge_mcp_config({}, _CONFIG_PATH)
    second = merge_mcp_config(first, _CONFIG_PATH)
    assert first == second


def test_merge_mcp_config_rejects_foreign_forseti_named_server() -> None:
    existing = {
        "mcpServers": {"forseti": {"command": "npx", "args": ["something-else"]}}
    }
    with pytest.raises(ProjectConfigError, match="does not look like forseti's own"):
        merge_mcp_config(existing, _CONFIG_PATH)


def test_merge_mcp_config_rejects_non_object_mcp_servers() -> None:
    with pytest.raises(ProjectConfigError, match="must be an object"):
        merge_mcp_config({"mcpServers": []}, _CONFIG_PATH)


def test_strip_mcp_server_drops_only_forseti_entry() -> None:
    existing = {
        "mcpServers": {
            "forseti": {"command": "forseti", "args": ["mcp"]},
            "other": {"command": "npx", "args": ["other"]},
        }
    }
    stripped = install_module._strip_mcp_server(existing, _CONFIG_PATH)
    assert stripped["mcpServers"] == {"other": {"command": "npx", "args": ["other"]}}


def test_strip_mcp_server_drops_empty_mcp_servers_key() -> None:
    existing = {"mcpServers": {"forseti": {"command": "forseti", "args": ["mcp"]}}}
    stripped = install_module._strip_mcp_server(existing, _CONFIG_PATH)
    assert "mcpServers" not in stripped


def test_strip_mcp_server_rejects_foreign_forseti_named_server() -> None:
    existing = {
        "mcpServers": {"forseti": {"command": "npx", "args": ["something-else"]}}
    }
    with pytest.raises(ProjectConfigError, match="does not look like forseti's own"):
        install_module._strip_mcp_server(existing, _CONFIG_PATH)


def test_strip_mcp_server_rejects_non_object_mcp_servers() -> None:
    with pytest.raises(ProjectConfigError, match="must be an object"):
        install_module._strip_mcp_server({"mcpServers": []}, _CONFIG_PATH)


def test_strip_mcp_server_noop_when_absent() -> None:
    existing = {"mcpServers": {"other": {"command": "npx"}}}
    assert install_module._strip_mcp_server(existing, _CONFIG_PATH) == existing


# --- install / remove -------------------------------------------------------


def test_install_creates_fresh_extension_and_mcp_config(tmp_path: Path) -> None:
    path, outcome = install(tmp_path)
    assert outcome is InstallOutcome.CREATED
    assert path == tmp_path / ".omp"

    ext_path = tmp_path / ".omp" / "extensions" / "forseti-gate.ts"
    assert ext_path.read_text() == install_module._EXTENSION_SOURCE

    mcp_path = tmp_path / ".omp" / "mcp.json"
    mcp_config = json.loads(mcp_path.read_text())
    assert mcp_config["mcpServers"]["forseti"] == {
        "command": "forseti",
        "args": ["mcp"],
    }


def test_install_rerun_reports_already_up_to_date(tmp_path: Path) -> None:
    install(tmp_path)
    _path, outcome = install(tmp_path)
    assert outcome is InstallOutcome.UNCHANGED


def test_install_on_existing_mcp_config_preserves_other_servers(tmp_path: Path) -> None:
    mcp_path = tmp_path / ".omp" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "npx", "args": ["x"]}}})
    )
    _path, outcome = install(tmp_path)
    assert outcome is InstallOutcome.UPDATED
    config = json.loads(mcp_path.read_text())
    assert config["mcpServers"]["other"] == {"command": "npx", "args": ["x"]}
    assert config["mcpServers"]["forseti"] == {"command": "forseti", "args": ["mcp"]}


def test_install_on_stale_forseti_extension_reports_updated(tmp_path: Path) -> None:
    ext_path = tmp_path / ".omp" / "extensions" / "forseti-gate.ts"
    ext_path.parent.mkdir(parents=True)
    ext_path.write_text(
        install_module._MARKER_START + "\n// an older forseti version\n"
    )
    _path, outcome = install(tmp_path)
    assert outcome is InstallOutcome.UPDATED
    assert ext_path.read_text() == install_module._EXTENSION_SOURCE


def test_install_on_non_object_mcp_json_raises(tmp_path: Path) -> None:
    mcp_path = tmp_path / ".omp" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ProjectConfigError, match="must be an object"):
        install(tmp_path)


def test_install_refuses_to_overwrite_hand_written_extension(tmp_path: Path) -> None:
    ext_path = tmp_path / ".omp" / "extensions" / "forseti-gate.ts"
    ext_path.parent.mkdir(parents=True)
    ext_path.write_text("// my own extension, nothing to do with forseti\n")
    with pytest.raises(ProjectConfigError, match="does not look like forseti's own"):
        install(tmp_path)
    assert "my own extension" in ext_path.read_text()


def test_install_refuses_to_overwrite_foreign_mcp_server(tmp_path: Path) -> None:
    mcp_path = tmp_path / ".omp" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        json.dumps({"mcpServers": {"forseti": {"command": "npx", "args": ["not-us"]}}})
    )
    with pytest.raises(ProjectConfigError, match="does not look like forseti's own"):
        install(tmp_path)


def test_install_on_malformed_mcp_json_raises(tmp_path: Path) -> None:
    mcp_path = tmp_path / ".omp" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text("not json")
    with pytest.raises(ProjectConfigError):
        install(tmp_path)


def test_install_on_extension_symlink_raises(tmp_path: Path) -> None:
    real = tmp_path / "real.ts"
    real.write_text("")
    ext_dir = tmp_path / ".omp" / "extensions"
    ext_dir.mkdir(parents=True)
    (ext_dir / "forseti-gate.ts").symlink_to(real)
    with pytest.raises(ProjectConfigError, match="symlink"):
        install(tmp_path)


def test_install_on_mcp_config_symlink_raises(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}")
    omp_dir = tmp_path / ".omp"
    omp_dir.mkdir()
    (omp_dir / "mcp.json").symlink_to(real)
    with pytest.raises(ProjectConfigError, match="symlink"):
        install(tmp_path)


def test_remove_on_missing_project_is_noop_and_creates_nothing(tmp_path: Path) -> None:
    path, outcome = remove(tmp_path)
    assert outcome is RemoveOutcome.UNCHANGED
    assert not path.exists()


def test_remove_deletes_extension_and_strips_mcp_entry(tmp_path: Path) -> None:
    install(tmp_path)
    path, outcome = remove(tmp_path)
    assert outcome is RemoveOutcome.REMOVED
    assert not (tmp_path / ".omp" / "extensions" / "forseti-gate.ts").exists()
    mcp_config = json.loads((tmp_path / ".omp" / "mcp.json").read_text())
    assert "mcpServers" not in mcp_config
    assert path == tmp_path / ".omp"


def test_remove_preserves_unrelated_mcp_servers(tmp_path: Path) -> None:
    install(tmp_path)
    mcp_path = tmp_path / ".omp" / "mcp.json"
    config = json.loads(mcp_path.read_text())
    config["mcpServers"]["other"] = {"command": "npx", "args": ["other"]}
    mcp_path.write_text(json.dumps(config))

    remove(tmp_path)
    remaining = json.loads(mcp_path.read_text())
    assert remaining["mcpServers"] == {"other": {"command": "npx", "args": ["other"]}}


def test_remove_again_reports_unchanged(tmp_path: Path) -> None:
    install(tmp_path)
    remove(tmp_path)
    _path, outcome = remove(tmp_path)
    assert outcome is RemoveOutcome.UNCHANGED


def test_remove_refuses_foreign_extension(tmp_path: Path) -> None:
    ext_path = tmp_path / ".omp" / "extensions" / "forseti-gate.ts"
    ext_path.parent.mkdir(parents=True)
    ext_path.write_text("// my own extension\n")
    with pytest.raises(ProjectConfigError, match="does not look like forseti's own"):
        remove(tmp_path)
    assert ext_path.exists()


def test_remove_on_extension_symlink_raises(tmp_path: Path) -> None:
    real = tmp_path / "real.ts"
    real.write_text(install_module._EXTENSION_SOURCE)
    ext_dir = tmp_path / ".omp" / "extensions"
    ext_dir.mkdir(parents=True)
    (ext_dir / "forseti-gate.ts").symlink_to(real)
    with pytest.raises(ProjectConfigError, match="symlink"):
        remove(tmp_path)
    assert real.is_file()
    assert not real.is_symlink()


def test_remove_on_mcp_config_symlink_raises(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}")
    omp_dir = tmp_path / ".omp"
    omp_dir.mkdir()
    (omp_dir / "mcp.json").symlink_to(real)
    with pytest.raises(ProjectConfigError, match="symlink"):
        remove(tmp_path)
    assert real.is_file()
    assert not real.is_symlink()
