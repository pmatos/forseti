from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path
from zipfile import ZipFile


def test_prepare_release_synchronizes_source_and_wheel_version(
    tmp_path: Path,
) -> None:
    project = tmp_path / "forseti"
    project.mkdir()
    shutil.copy("pyproject.toml", project / "pyproject.toml")
    shutil.copytree("src", project / "src")

    script = Path(".github/semantic-release/prepare.sh").resolve()
    completed = subprocess.run(
        ["bash", str(script), "1.8.0"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    metadata = tomllib.loads((project / "pyproject.toml").read_text())
    assert metadata["project"]["version"] == "1.8.0"

    wheel = project / "dist/forseti-1.8.0-py3-none-any.whl"
    assert wheel.is_file()
    with ZipFile(wheel) as archive:
        wheel_metadata = archive.read("forseti-1.8.0.dist-info/METADATA").decode()
    assert "\nVersion: 1.8.0\n" in wheel_metadata


def test_prepare_release_rejects_a_non_release_version_without_mutating_source(
    tmp_path: Path,
) -> None:
    project = tmp_path / "forseti"
    project.mkdir()
    pyproject = project / "pyproject.toml"
    shutil.copy("pyproject.toml", pyproject)
    before = pyproject.read_text()

    script = Path(".github/semantic-release/prepare.sh").resolve()
    completed = subprocess.run(
        ["bash", str(script), "1.8"],
        cwd=project,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "MAJOR.MINOR.PATCH" in completed.stderr
    assert pyproject.read_text() == before
    assert not (project / "dist").exists()
