#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 || ! $1 =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  printf 'usage: %s MAJOR.MINOR.PATCH\n' "$0" >&2
  exit 2
fi

FORSETI_RELEASE_VERSION=$1

python - "$FORSETI_RELEASE_VERSION" <<'PY'
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

version = sys.argv[1]
path = Path("pyproject.toml")
lines = path.read_text().splitlines(keepends=True)
in_project = False
version_lines: list[int] = []
pattern = re.compile(r'^(\s*version\s*=\s*")[^"]+(".*(?:\n)?)$')

for index, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        in_project = stripped == "[project]"
        continue
    if in_project and pattern.fullmatch(line) is not None:
        version_lines.append(index)

if len(version_lines) != 1:
    raise SystemExit(
        "expected exactly one version field in [project], "
        f"found {len(version_lines)}"
    )

index = version_lines[0]
match = pattern.fullmatch(lines[index])
assert match is not None
lines[index] = f"{match.group(1)}{version}{match.group(2)}"
path.write_text("".join(lines))

if tomllib.loads(path.read_text())["project"]["version"] != version:
    raise SystemExit("updated pyproject.toml does not contain the requested version")
PY

rm -rf -- dist
python -m build --wheel --no-isolation --outdir dist

python - "$FORSETI_RELEASE_VERSION" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path
from zipfile import ZipFile

version = sys.argv[1]
expected = Path("dist") / f"forseti-{version}-py3-none-any.whl"
wheels = list(Path("dist").glob("*.whl"))
if wheels != [expected]:
    raise SystemExit(
        f"expected exactly {expected}, found {', '.join(map(str, wheels)) or 'no wheel'}"
    )

with ZipFile(expected) as archive:
    metadata = archive.read(f"forseti-{version}.dist-info/METADATA").decode()
if f"\nVersion: {version}\n" not in metadata:
    raise SystemExit(f"wheel metadata does not contain version {version}")
PY
