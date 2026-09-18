#!/usr/bin/env bash
# Materialize a fresh, isolated demo workspace for the Forseti write -> verify
# -> counterexample -> fix demo.
#
# Usage: init.sh <target-dir>
#
# Creates <target-dir> as its own git repo (with an initial empty commit, so
# Forseti's git-based out-of-band scan has a HEAD to diff against), drops in
# the demo CLAUDE.md workflow contract, and installs Forseti's Claude Code
# verify-gate hooks (SessionStart/PostToolUse/Stop) into
# <target-dir>/.claude/settings.local.json via `forseti enable-project`.
#
# Deliberately reads nothing from the forseti repo's own examples/ directory:
# the whole point of <target-dir> is that a Claude session started there
# cannot see this repo's pre-staged bug corpus.
#
# Safe to call repeatedly with different (or the same) <target-dir>.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_MD_TEMPLATE="${SCRIPT_DIR}/CLAUDE.md"

usage() {
    echo "usage: $(basename "$0") <target-dir>" >&2
}

if [[ $# -ne 1 ]]; then
    usage
    exit 1
fi

TARGET_DIR="$1"

if [[ -z "${TARGET_DIR}" ]]; then
    echo "error: <target-dir> must not be empty" >&2
    usage
    exit 1
fi

# Fail loudly rather than silently proceeding without the verify-gate hooks
# (repo convention: never silently pass).
if ! command -v forseti >/dev/null 2>&1; then
    echo "error: 'forseti' is not on PATH. Run 'source demo/env.sh' first (this" >&2
    echo "script refuses to build a demo workspace with no verify-gate hooks)." >&2
    exit 1
fi

if [[ ! -f "${CLAUDE_MD_TEMPLATE}" ]]; then
    echo "error: missing template ${CLAUDE_MD_TEMPLATE}" >&2
    exit 1
fi

mkdir -p "${TARGET_DIR}"

git -C "${TARGET_DIR}" init -q

# Local, per-workspace identity for the initial commit only -- don't assume
# the machine running this has git user.name/user.email configured globally,
# and don't touch that global config.
if [[ -z "$(git -C "${TARGET_DIR}" config --get user.email || true)" ]]; then
    git -C "${TARGET_DIR}" config user.email "forseti-demo@localhost"
fi
if [[ -z "$(git -C "${TARGET_DIR}" config --get user.name || true)" ]]; then
    git -C "${TARGET_DIR}" config user.name "Forseti Demo"
fi

if ! git -C "${TARGET_DIR}" rev-parse --verify -q HEAD >/dev/null; then
    git -C "${TARGET_DIR}" commit -q --allow-empty -m "Initial empty commit (demo workspace scaffold)"
fi

cp "${CLAUDE_MD_TEMPLATE}" "${TARGET_DIR}/CLAUDE.md"

forseti enable-project "${TARGET_DIR}" --harness claude-code

echo "Demo workspace ready at ${TARGET_DIR}"
