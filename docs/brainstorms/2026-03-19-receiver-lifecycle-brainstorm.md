# Brainstorm: Receiver Lifecycle & Containerization

**Date:** 2026-03-19
**Status:** Decided

## What We're Building

Fix the receiver's broken shutdown behavior, add proper start/stop scripts, and clean up the dashboard's orphan container warning.

### Problems Being Solved

1. **Receiver won't shut down cleanly.** `metrics.py` registers a SIGTERM handler that shadows the main shutdown handler in `__main__.py`. When SIGTERM/SIGINT arrives, the metrics handler fires (last registered wins), raises `SystemExit(0)` immediately, and the real cleanup (stop dispatcher, shutdown HTTP server, close event logger) never runs. Threads and worker subprocesses stay alive.

2. **Orphan container warning.** Dashboard's `docker compose up` reports `Found orphan containers ([claude-node-exporter])` — a leftover from a removed service definition.

3. **No unified lifecycle.** Receiver runs as a bare Python process while dashboard runs in Docker Compose. Two separate start/stop workflows.

## Why This Approach

**Fix signals + start/stop scripts** — the receiver must stay as a bare Python process because it spawns `claude --print` subprocesses that use OAuth sessions from the host. Containerizing would require solving CLI auth, repo access, and networking — not worth it.

Instead: fix the signal handler conflict so SIGTERM/SIGINT work correctly, then add `start.sh`/`stop.sh` scripts (matching the dashboard's pattern) for clean lifecycle management.

Rejected alternatives:
- **Containerize in dashboard compose**: Too complex — receiver needs OAuth sessions, git repos, host networking
- **Separate compose stack**: Same containerization problems
- **Systemd unit**: Different paradigm from dashboard, not portable, overkill

## Key Decisions

1. **Single signal handler in `__main__.py`** — Remove the competing SIGTERM handler from `metrics.py`. Metrics persistence (`save_state()`) gets called as part of the main shutdown sequence, not via its own signal handler. Keep `atexit` as a safety net.

2. **Start/stop scripts** — `start.sh` launches receiver in background, writes PID file. `stop.sh` reads PID file, sends SIGTERM, waits for clean exit. Matches the dashboard's pattern.

3. **Orphan cleanup** — Dashboard's `start.sh` gets `--remove-orphans` flag on `docker compose up`.

4. **Receiver stays on host** — Cannot containerize because it spawns `claude --print` subprocesses that use OAuth sessions from the host environment.

## Files to Change

| File | Change |
|------|--------|
| `receiver/metrics.py` | Remove SIGTERM handler, keep `atexit` only |
| `receiver/__main__.py` | Single clean shutdown path (no duplicate finally block) |
| `start.sh` (new, in bootstrap repo) | Launch receiver in background, write PID file |
| `stop.sh` (new, in bootstrap repo) | Read PID, send SIGTERM, wait for clean exit |
| `~/claude-agent-dashboard/start.sh` | Add `--remove-orphans` to `docker compose up` |
