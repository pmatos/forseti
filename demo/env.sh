# Source this before running any demo step (init.sh, or launching `claude`):
#
#   source demo/env.sh
#
# Puts, in order: demo/bin (the CLI event-logging shim) and this repo's own
# .venv/bin (an editable install, so it reflects whatever is on this branch --
# critically, any fix merged to main but not yet in a released package) ahead
# of everything else on PATH. Without this, `forseti` on a typical dev
# machine resolves to a separately-installed release build (e.g. a `uv tool
# install`), which can silently lag behind this checkout.
#
# Must be sourced, not executed: it edits the calling shell's PATH.

# Portable self-path detection: this must work when sourced from bash
# (`${BASH_SOURCE[0]}`) or zsh (`${(%):-%N}`, since zsh does not reliably
# populate `$BASH_SOURCE` for a sourced script) -- both are named as the
# supported dev shell in this environment.
if [ -n "${BASH_SOURCE:-}" ]; then
    DEMO_ENV_SELF="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
    DEMO_ENV_SELF="${(%):-%N}"
else
    DEMO_ENV_SELF="$0"
fi
DEMO_ENV_SCRIPT_DIR="$(cd "$(dirname "${DEMO_ENV_SELF}")" && pwd)"
unset DEMO_ENV_SELF
FORSETI_REPO_ROOT="$(cd "${DEMO_ENV_SCRIPT_DIR}/.." && pwd)"

if [[ ! -x "${FORSETI_REPO_ROOT}/.venv/bin/forseti" ]]; then
    echo "demo/env.sh: no editable install at ${FORSETI_REPO_ROOT}/.venv -- run" >&2
    echo "  pip install -e '.[dev]'" >&2
    echo "from the repo root first (or: uv venv && uv pip install -e '.[dev]')." >&2
    return 1 2>/dev/null || exit 1
fi

export PATH="${DEMO_ENV_SCRIPT_DIR}/bin:${FORSETI_REPO_ROOT}/.venv/bin:${PATH}"

unset DEMO_ENV_SCRIPT_DIR FORSETI_REPO_ROOT
