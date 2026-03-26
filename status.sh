#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${HOME}/.claude/agent-receiver.pid"

# --- Receiver status ---
receiver_exit=2
status_line="STATUS=not_running"

if [[ -f "$PID_FILE" ]]; then
    pid=$(cat "$PID_FILE")
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        if curl -sf http://localhost:9876/health >/dev/null 2>&1; then
            status_line="STATUS=healthy PID=$pid"
            receiver_exit=0
        else
            status_line="STATUS=unhealthy PID=$pid"
            receiver_exit=1
        fi
    fi
fi

# --- Dashboard status ---
if [[ -f "${HOME}/.claude/agent-dashboard.enabled" ]]; then
    COMPOSE_FILE="${SCRIPT_DIR}/observability/docker-compose.yml"
    if command -v docker &>/dev/null && [[ -f "$COMPOSE_FILE" ]]; then
        RUNNING=$(docker compose -f "$COMPOSE_FILE" ps --status running --quiet 2>/dev/null | wc -l)
        TOTAL=$(docker compose -f "$COMPOSE_FILE" config --services 2>/dev/null | wc -l)
        if [[ "$RUNNING" -eq "$TOTAL" && "$TOTAL" -gt 0 ]]; then
            status_line+=" DASHBOARD=running URL=http://localhost:3000"
        else
            status_line+=" DASHBOARD=not_running"
        fi
    fi
fi

echo "$status_line"
exit "$receiver_exit"
