#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${HOME}/.claude/agent-receiver.pid"
LOG_FILE="${HOME}/.claude/receiver.log"

# Serialize access to PID file (prevents TOCTOU race on concurrent start)
exec 200>"${PID_FILE}.lock"
flock -n 200 || { echo "Another start.sh is already running"; exit 1; }

# Check if already running
if [[ -f "$PID_FILE" ]]; then
    pid=$(cat "$PID_FILE")
    if [[ "$pid" =~ ^[0-9]+$ ]] && [[ "$pid" -gt 0 ]] && kill -0 "$pid" 2>/dev/null; then
        echo "Receiver already running (PID: $pid)"
        exit 0
    fi
    rm -f "$PID_FILE"  # Stale PID file
fi

# Preflight checks
if [[ ! -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    echo "ERROR: .venv not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

mkdir -p "${HOME}/.claude"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"

cd "$SCRIPT_DIR"
"${SCRIPT_DIR}/.venv/bin/python" -m receiver >> "$LOG_FILE" 2>&1 200>&- &
echo $! > "$PID_FILE"
exec 200>&-  # Release flock before potentially slow Docker operations

# Verify startup succeeded
sleep 2
if ! kill -0 $! 2>/dev/null; then
    echo "ERROR: Receiver failed to start. Check log: $LOG_FILE"
    tail -5 "$LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi

echo "Receiver started (PID: $!, log: $LOG_FILE)"

# --- JSONL rotation (prevent unbounded growth) ---
EVENTS_FILE="${HOME}/.claude/agent-events.jsonl"
MAX_SIZE=$((10 * 1024 * 1024))  # 10 MiB
if [[ -f "$EVENTS_FILE" ]] && [[ $(stat -c%s "$EVENTS_FILE" 2>/dev/null || echo 0) -gt $MAX_SIZE ]]; then
    mv "$EVENTS_FILE" "${EVENTS_FILE}.1"
fi
touch "$EVENTS_FILE"

# --- Dashboard (observability stack) ---
if [[ -f "${HOME}/.claude/agent-dashboard.enabled" ]]; then
    COMPOSE_FILE="${SCRIPT_DIR}/observability/docker-compose.yml"
    if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
        export EVENTS_FILE
        # Background on first run (image pull takes minutes)
        if ! docker image inspect grafana/grafana:11.6.0 &>/dev/null 2>&1; then
            echo "Pulling dashboard images (first run, ~1.7GB)... runs in background."
            docker compose -f "$COMPOSE_FILE" up -d --remove-orphans &>/dev/null &
            echo "Dashboard starting in background. Check with: ./status.sh"
        else
            docker compose -f "$COMPOSE_FILE" up -d --remove-orphans 2>&1 \
                && echo "Dashboard started (Grafana: http://localhost:3000)" \
                || echo "WARNING: Dashboard failed to start (receiver is still running)" >&2
        fi
    else
        echo "WARNING: Docker not available, skipping dashboard (receiver is still running)" >&2
    fi
fi
