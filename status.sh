#!/usr/bin/env bash
set -euo pipefail
PID_FILE="${HOME}/.claude/agent-receiver.pid"

if [[ ! -f "$PID_FILE" ]]; then
    echo "STATUS=not_running"
    exit 2
fi

pid=$(cat "$PID_FILE")
if ! [[ "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
    echo "STATUS=not_running (stale PID file)"
    exit 2
fi

# Check health endpoint
if curl -sf http://localhost:9876/health >/dev/null 2>&1; then
    echo "STATUS=healthy PID=$pid"
    exit 0
else
    echo "STATUS=unhealthy PID=$pid (process alive but /health failed)"
    exit 1
fi
