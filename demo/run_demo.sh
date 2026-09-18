#!/usr/bin/env bash
# Launch the Forseti demo: an interactive `claude` session on the left,
# the live observability canvas on the right (a browser tab; a tmux pane
# running the textual demo/render.py view as an SSH/no-GUI fallback).
#
# Usage: run_demo.sh [target-dir] [--port N]
#
#   target-dir   demo workspace to use (default: a fresh dir under mktemp -d).
#                Scaffolded via demo/scaffold/init.sh if not already set up
#                (safe to re-run against an existing one).
#   --port N     canvas server port (default: 8765).
#
# Requires: this checkout's own forseti build (demo/env.sh, sourced
# automatically -- see its own error message if that fails), tmux (falls
# back to running claude directly in the current terminal if tmux is
# unavailable), and a browser for the canvas (falls back to printing the
# URL if nothing can open one -- xdg-open/open are best-effort, never fatal).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/env.sh"

PORT=8765
TARGET_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            PORT="${2:?--port requires a value}"
            shift 2
            ;;
        *)
            if [[ -n "${TARGET_DIR}" ]]; then
                echo "error: unexpected extra argument: $1" >&2
                exit 1
            fi
            TARGET_DIR="$1"
            shift
            ;;
    esac
done

if [[ -z "${TARGET_DIR}" ]]; then
    TARGET_DIR="$(mktemp -d -t forseti-demo-XXXXXX)"
    echo "No target-dir given -- using a fresh workspace: ${TARGET_DIR}"
else
    # Canonicalize a user-supplied target-dir to absolute right away: the
    # tmux render.py pane below is started with its cwd forced to the repo
    # root (not wherever this script was invoked from), so a relative path
    # here would resolve against the wrong base for that pane while still
    # resolving correctly for canvas/server.py and scaffold/init.sh (both
    # invoked from this same shell before any cwd changes).
    mkdir -p -- "${TARGET_DIR}"
    TARGET_DIR="$(cd -- "${TARGET_DIR}" && pwd)"
fi

bash "${SCRIPT_DIR}/scaffold/init.sh" "${TARGET_DIR}"

CANVAS_PID=""
cleanup() {
    if [[ -n "${CANVAS_PID}" ]] && kill -0 "${CANVAS_PID}" 2>/dev/null; then
        kill "${CANVAS_PID}" 2>/dev/null || true
        wait "${CANVAS_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

python3 "${SCRIPT_DIR}/canvas/server.py" "${TARGET_DIR}" --port "${PORT}" &
CANVAS_PID=$!
sleep 0.5
if ! kill -0 "${CANVAS_PID}" 2>/dev/null; then
    echo "error: the canvas server failed to start (port ${PORT} busy?)" >&2
    exit 1
fi

CANVAS_URL="http://localhost:${PORT}"
echo "Canvas: ${CANVAS_URL}"
if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${CANVAS_URL}" >/dev/null 2>&1 &
elif command -v open >/dev/null 2>&1; then
    open "${CANVAS_URL}" >/dev/null 2>&1 &
else
    echo "(no xdg-open/open found -- open the URL above manually)"
fi

if command -v tmux >/dev/null 2>&1 && [[ -z "${TMUX:-}" ]]; then
    SESSION="forseti-demo-$$"
    # Re-source env.sh inside each pane rather than relying on tmux to carry
    # this shell's just-modified PATH into it: `new-session`/`split-window`
    # only inherit the *server's* environment (captured whenever the tmux
    # server itself first started), not this shell's current one, unless a
    # server is started fresh right here. A dev with an already-running tmux
    # server (a common state) would otherwise silently get whatever `forseti`
    # was on PATH at server-start time -- a separately-installed release
    # build, not this checkout -- defeating env.sh's whole purpose. Each
    # `printf %q` shell-quotes its argument so a TARGET_DIR/SCRIPT_DIR
    # containing spaces or quote characters still round-trips correctly
    # through tmux's own `$SHELL -c` re-parse of the pane command string.
    ENV_SH_Q="$(printf '%q' "${SCRIPT_DIR}/env.sh")"
    tmux new-session -d -s "${SESSION}" -c "${TARGET_DIR}" \
        "bash -c \"source ${ENV_SH_Q} && exec claude\""
    RENDER_CMD="source ${ENV_SH_Q} && exec python3 $(printf '%q' "${SCRIPT_DIR}/render.py") $(printf '%q' "${TARGET_DIR}")"
    tmux split-window -h -t "${SESSION}" -c "${SCRIPT_DIR}/.." "bash -c \"${RENDER_CMD}\""
    tmux select-pane -t "${SESSION}.0"
    echo "tmux session: ${SESSION} (left: claude, right: render.py)"
    # `tmux attach` fails outright with no controlling TTY (e.g. this script
    # itself launched non-interactively) -- degrade to a clear message rather
    # than letting `set -e` tear the canvas down via the EXIT trap. Detaching
    # normally (or the session ending) still returns here as success.
    if ! tmux attach -t "${SESSION}"; then
        echo "could not attach (no TTY?) -- reattach yourself with:" >&2
        echo "  tmux attach -t ${SESSION}" >&2
    fi
else
    echo "(tmux unavailable or already inside one -- starting claude directly;"
    echo " run 'python3 ${SCRIPT_DIR}/render.py ${TARGET_DIR}' in another"
    echo " terminal for the textual view.)"
    cd "${TARGET_DIR}"
    # Not `exec`: replacing this shell's process image would skip the EXIT
    # trap above, leaking the background canvas server once claude exits.
    claude
fi
