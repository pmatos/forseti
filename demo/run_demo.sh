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
            PORT="$2"
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
    tmux new-session -d -s "${SESSION}" -c "${TARGET_DIR}" "claude"
    tmux split-window -h -t "${SESSION}" -c "${SCRIPT_DIR}/.." \
        "python3 ${SCRIPT_DIR}/render.py '${TARGET_DIR}'"
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
    exec claude
fi
