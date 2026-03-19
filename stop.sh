#!/usr/bin/env bash
set -euo pipefail
PID_FILE="${HOME}/.claude/agent-receiver.pid"

# Serialize access to PID file
exec 200>"${PID_FILE}.lock"
flock -n 200 || { echo "Another stop/start is already running"; exit 1; }

if [[ ! -f "$PID_FILE" ]]; then
    echo "Receiver is not running (no PID file)"
    exit 0
fi

pid=$(cat "$PID_FILE")

# Validate PID is a positive integer
if ! [[ "$pid" =~ ^[0-9]+$ ]] || [[ "$pid" -eq 0 ]]; then
    echo "Invalid PID file contents: $pid"
    rm -f "$PID_FILE"
    exit 1
fi

# Validate PID belongs to receiver (cross-platform: works on Linux + macOS)
if ! ps -p "$pid" -o args= 2>/dev/null | grep -q "receiver"; then
    echo "Stale PID file (PID $pid is not the receiver)"
    rm -f "$PID_FILE"
    exit 0
fi

kill "$pid" 2>/dev/null || { echo "Process already stopped"; rm -f "$PID_FILE"; exit 0; }
echo "Stopping receiver (PID: $pid)..."

# Wait up to 45s for clean exit (aligns with 30s dispatcher timeout + 5s heartbeat + buffer)
for i in $(seq 1 45); do
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "Receiver stopped cleanly"
        exit 0
    fi
    sleep 1
done

# Escalate to SIGKILL
echo "Receiver did not exit cleanly, sending SIGKILL..."
kill -9 "$pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "Receiver killed"
exit 1
