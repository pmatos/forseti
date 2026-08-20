from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile


def test_built_distribution_has_the_project_release_version(tmp_path: Path) -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    project_version = project["project"]["version"]

    assert re.fullmatch(r"\d+\.\d+\.\d+", project_version)
    assert project_version != "0.0.0"

    project_copy = tmp_path / "forseti"
    project_copy.mkdir()
    shutil.copy("pyproject.toml", project_copy / "pyproject.toml")
    shutil.copytree("src", project_copy / "src")
    wheel_directory = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(wheel_directory),
        ],
        cwd=project_copy,
        check=True,
    )

    wheel = wheel_directory / f"forseti-{project_version}-py3-none-any.whl"
    assert wheel.is_file()
    with ZipFile(wheel) as archive:
        metadata_name = f"forseti-{project_version}.dist-info/METADATA"
        metadata = archive.read(metadata_name).decode()
    assert f"\nVersion: {project_version}\n" in metadata
