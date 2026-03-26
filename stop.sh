#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${HOME}/.claude/agent-receiver.pid"
receiver_exit=0

# Serialize access to PID file
exec 200>"${PID_FILE}.lock"
flock -n 200 || { echo "Another stop/start is already running"; exit 1; }

if [[ ! -f "$PID_FILE" ]]; then
    echo "Receiver is not running (no PID file)"
else
    pid=$(cat "$PID_FILE")

    # Validate PID is a positive integer
    if ! [[ "$pid" =~ ^[0-9]+$ ]] || [[ "$pid" -eq 0 ]]; then
        echo "Invalid PID file contents: $pid"
        rm -f "$PID_FILE"
        receiver_exit=1
    # Validate PID belongs to receiver (cross-platform: works on Linux + macOS)
    elif ! ps -p "$pid" -o args= 2>/dev/null | grep -q "receiver"; then
        echo "Stale PID file (PID $pid is not the receiver)"
        rm -f "$PID_FILE"
    else
        kill "$pid" 2>/dev/null || { echo "Process already stopped"; rm -f "$PID_FILE"; pid=""; }

        if [[ -n "${pid:-}" ]]; then
            echo "Stopping receiver (PID: $pid)..."

            # Wait up to 45s for clean exit (aligns with 30s dispatcher timeout + 5s heartbeat + buffer)
            stopped=false
            for i in $(seq 1 45); do
                if ! kill -0 "$pid" 2>/dev/null; then
                    rm -f "$PID_FILE"
                    echo "Receiver stopped cleanly"
                    stopped=true
                    break
                fi
                sleep 1
            done

            if [[ "$stopped" != "true" ]]; then
                # Escalate to SIGKILL
                echo "Receiver did not exit cleanly, sending SIGKILL..."
                kill -9 "$pid" 2>/dev/null || true
                rm -f "$PID_FILE"
                echo "Receiver killed"
                receiver_exit=1
            fi
        fi
    fi
fi

exec 200>&-  # Release flock

# --- Stop dashboard containers (if running) ---
if command -v docker &>/dev/null; then
    COMPOSE_FILE="${SCRIPT_DIR}/observability/docker-compose.yml"
    if [[ -f "$COMPOSE_FILE" ]] && docker compose -f "$COMPOSE_FILE" ps --quiet 2>/dev/null | grep -q .; then
        docker compose -f "$COMPOSE_FILE" down 2>&1 \
            && echo "Dashboard stopped" \
            || echo "WARNING: Failed to stop dashboard containers" >&2
    fi
fi

exit "$receiver_exit"
