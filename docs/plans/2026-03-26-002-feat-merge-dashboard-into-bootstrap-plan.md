---
title: "feat: Merge Dashboard into Bootstrap with Unified Lifecycle"
type: feat
status: active
date: 2026-03-26
origin: docs/brainstorms/2026-03-26-merge-dashboard-into-bootstrap-brainstorm.md
---

# feat: Merge Dashboard into Bootstrap with Unified Lifecycle

## Enhancement Summary (2026-03-26)

### Key Improvements
1. **Sentinel file replaces config file** -- eliminates `source` security risk and simplifies all scripts
2. **`name: claude-agent` in compose file** -- replaces fragile `-p` flag on every invocation
3. **Explicit volume names in compose** -- prevents data loss during migration (critical fix)
4. **JSONL rotation** -- prevents unbounded growth and Promtail OOM
5. **Simplified Docker install** -- replaced 20-line function with 3-line check-and-link
6. **Removed migration function** -- self-correcting via container name conflicts
7. **Explicit flock release** -- `exec 200>&-` before Docker section
8. **Background Docker pull on first run** -- eliminates 5-minute hang
9. **Prometheus retention 365d → 90d** -- saves 1-4 GiB disk

### Fixes from Review
- `setup.sh` path corrected (repo root, not `scripts/`)
- `stop.sh` code block now matches narrative (receiver-first ordering)
- `status.sh` output folded into single line (preserves machine-parseable contract)
- `TOTAL` container count derived dynamically (not hardcoded)
- Compose file `EVENTS_FILE` default changed to fail-fast (no tilde)

---

## Overview

Merge the `claude-agent-dashboard` repo into `claude-agent-bootstrap` as an `observability/` subdirectory. Unify the lifecycle scripts (`start.sh`, `stop.sh`, `status.sh`) to manage both the receiver and the observability stack (Grafana + Loki + Promtail + Prometheus) as a single unit. The dashboard is opt-in during `setup.sh` and persisted as a global sentinel file.

## Problem Statement / Motivation

The dashboard repo has no independent value -- it only exists to visualize bootstrap's JSONL events and scrape its `/metrics` endpoint. Keeping it separate means two repos to clone, two sets of start/stop scripts, and an awkward "clone the other repo" prompt in setup.sh. (see brainstorm: docs/brainstorms/2026-03-26-merge-dashboard-into-bootstrap-brainstorm.md)

## Proposed Solution

Copy dashboard files into `observability/`, extend existing lifecycle scripts with conditional Docker management, and add an interactive dashboard prompt to setup.sh.

## Technical Considerations

### Dashboard Enablement: Sentinel File

Use a sentinel file `~/.claude/agent-dashboard.enabled`. File exists = enabled, absent = disabled. Zero parsing, zero sourcing, zero security risk. (A sourced config file was rejected because `source` executes arbitrary code — HIGH severity per security review.)

```bash
# Check: is dashboard enabled?
if [[ -f "${HOME}/.claude/agent-dashboard.enabled" ]]; then
    # start docker stack
fi

# Enable:  touch ~/.claude/agent-dashboard.enabled
# Disable: rm ~/.claude/agent-dashboard.enabled
```

This also eliminates the `GRAFANA_PORT` config variable. The compose file owns the port; a config file that disagrees with the compose file is a lie. If port customization is needed later, it goes in the compose file or a `.env` file next to it.

### Scripts Stay at Root

All lifecycle scripts (`start.sh`, `stop.sh`, `status.sh`, `setup.sh`) live at the repo root. The brainstorm proposed `scripts/`, but moving them breaks `SCRIPT_DIR` path resolution and existing users.

### Compose File: `name:` Field + Explicit Volume Names (Critical)

Set `name: claude-agent` at the top of the compose file and use explicit `name:` on each volume. This avoids requiring a `-p` flag on every invocation (fragile — forgetting it creates orphan containers) and decouples volume names from the project name (prevents data loss when directory paths change).

```yaml
# observability/docker-compose.yml
name: claude-agent

services:
  # ...

volumes:
  loki-data:
    name: claude-agent-loki-data
  grafana-data:
    name: claude-agent-grafana-data
  prometheus-data:
    name: claude-agent-prometheus-data
  promtail-positions:
    name: claude-agent-promtail-positions
```

With `name:` set in the file, all invocations just need `-f observability/docker-compose.yml` -- no `-p` flag. Volume names are project-name-independent, so they survive any future directory moves.

**Migration note for existing users:** Old volumes (`claude-agent-dashboard_*`) require a one-time copy documented in the README:

```bash
for vol in loki-data grafana-data prometheus-data promtail-positions; do
    docker volume create "claude-agent-${vol}" 2>/dev/null
    docker run --rm -v "claude-agent-dashboard_${vol}:/from:ro" -v "claude-agent-${vol}:/to" alpine sh -c 'cp -a /from/. /to/'
done
```

### Docker Auto-Install: Check-and-Link

If Docker is missing, print the install URL and return. Docker's own docs handle platform detection far better than a bash function, and `curl | sudo sh` is a security risk.

```bash
if ! command -v docker &>/dev/null; then
    echo "Docker not found. Install it: https://docs.docker.com/get-docker/"
    return
fi
```

### Flock Release: Explicit `exec 200>&-`

The current `start.sh` acquires a flock via `exec 200>"${PID_FILE}.lock"`. The fd stays open for the script's lifetime. The plan adds Docker operations after the receiver start, but without explicitly closing fd 200, the flock is held during Docker pulls (minutes on first run), blocking concurrent `stop.sh`.

**Resolution:** Add `exec 200>&-` after the receiver PID is verified, before the Docker section.

### Partial Failure Semantics

Docker failures warn on stderr; receiver failures abort. Exit code reflects only receiver health.

### JSONL Rotation (New)

The events file (`~/.claude/agent-events.jsonl`) grows unbounded. At current rates: ~370 KiB/day, ~134 MiB/year. If Promtail restarts and must reprocess a large backlog, it will OOM against its 128M limit.

**Resolution:** Add simple rotation in `start.sh` before launching Docker:

```bash
EVENTS_FILE="${HOME}/.claude/agent-events.jsonl"
MAX_SIZE=$((10 * 1024 * 1024))  # 10 MiB
if [[ -f "$EVENTS_FILE" ]] && [[ $(stat -c%s "$EVENTS_FILE" 2>/dev/null || echo 0) -gt $MAX_SIZE ]]; then
    mv "$EVENTS_FILE" "${EVENTS_FILE}.1"
fi
touch "$EVENTS_FILE"
```

### Compose File Tweaks (New)

From review agents, apply these changes when copying `docker-compose.yml`:

1. **`EVENTS_FILE` default:** Change from `${EVENTS_FILE:-~/.claude/agent-events.jsonl}` to `${EVENTS_FILE:?EVENTS_FILE must be set}`. Tilde does not expand in Docker Compose. Fail-fast is safer than a silent wrong path.
2. **Prometheus retention:** Change `365d` → `90d`. Saves 1-4 GiB disk. 90 days is sufficient for a dev tool.
3. **Remove `--web.enable-lifecycle`** from Prometheus unless hot-reload is actively needed (exposes `/-/quit` endpoint to any local process).

## Proposed Structure

```
claude-agent-bootstrap/
  receiver/                    # (unchanged)
  observability/               # NEW: merged from claude-agent-dashboard
    docker-compose.yml         # Modified: name field, explicit volume names, tweaks
    loki-config.yml
    promtail-config.yml
    prometheus.yml
    provisioning/
      dashboards/
        provider.yml
        fleet-overview.json
        agent-dashboard.json
        pipeline-progress.json
      datasources/
        loki.yml
        prometheus.yml
  start.sh                     # MODIFIED: flock release, Docker lifecycle, JSONL rotation
  stop.sh                      # MODIFIED: Docker lifecycle after receiver stop
  status.sh                    # MODIFIED: dashboard status on same line
  setup.sh                     # MODIFIED: dashboard prompt (at repo root, NOT scripts/)
  templates/                   # (unchanged)
  tests/                       # (unchanged)
```

## Implementation Phases

### Phase 1: Copy and Modify Dashboard Files

Copy files from `~/claude-agent-dashboard/` into `observability/`:

- `docker-compose.yml` -- **modified** (8 changes, detailed below)
- `loki-config.yml` -- as-is
- `promtail-config.yml` -- as-is
- `prometheus.yml` -- as-is
- `provisioning/` -- entire directory tree, as-is

**Do NOT copy:** `start.sh`, `stop.sh`, `README.md`, `docs/`, `todos/`, `.git/`

**Exact compose file edits (source: `~/claude-agent-dashboard/docker-compose.yml`):**

| # | Line | Change | Old | New |
|---|------|--------|-----|-----|
| 1 | 1 (insert before `services:`) | Add top-level name | *(none)* | `name: claude-agent` |
| 2 | 23 | EVENTS_FILE fail-fast | `${EVENTS_FILE:-~/.claude/agent-events.jsonl}` | `${EVENTS_FILE:?EVENTS_FILE must be set}` |
| 3 | 60 | Prometheus retention | `'--storage.tsdb.retention.time=365d'` | `'--storage.tsdb.retention.time=90d'` |
| 4 | 62 | Remove lifecycle endpoint | `'--web.enable-lifecycle'` | *(delete line)* |
| 5 | 102 | Loki volume name | `loki-data:` | `loki-data:\n    name: claude-agent-loki-data` |
| 6 | 103 | Grafana volume name | `grafana-data:` | `grafana-data:\n    name: claude-agent-grafana-data` |
| 7 | 104 | Prometheus volume name | `prometheus-data:` | `prometheus-data:\n    name: claude-agent-prometheus-data` |
| 8 | 105 | Promtail volume name | `promtail-positions:` | `promtail-positions:\n    name: claude-agent-promtail-positions` |

With `name: claude-agent` in the file, no `-p` flag is needed on any `docker compose` invocation. Volume names are project-name-independent and survive directory moves.

**Files to create:**
- `observability/docker-compose.yml`
- `observability/loki-config.yml`
- `observability/promtail-config.yml`
- `observability/prometheus.yml`
- `observability/provisioning/dashboards/provider.yml`
- `observability/provisioning/dashboards/fleet-overview.json`
- `observability/provisioning/dashboards/agent-dashboard.json`
- `observability/provisioning/dashboards/pipeline-progress.json`
- `observability/provisioning/datasources/loki.yml`
- `observability/provisioning/datasources/prometheus.yml`

### Phase 2: Extend `start.sh`

**Current file:** `start.sh` (44 lines, `set -euo pipefail`)

**`SCRIPT_DIR`** is set on line 3: `SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"`

**Insertion map** (no existing lines modified):

| Step | After line | What to insert |
|------|-----------|----------------|
| A | 33 (`echo $! > "$PID_FILE"`) | `exec 200>&-` (release flock — must be before the 2s sleep so `stop.sh` isn't blocked) |
| B | 42 (after startup verification `fi`) | JSONL rotation + sentinel check + Docker compose (entire block below) |

**Note:** The script uses `set -euo pipefail`. Docker commands must use `||` chains to prevent non-zero exits from killing the script.

**Insert after line 33:**

```bash
exec 200>&-  # Release flock before potentially slow Docker operations
```

**Insert after line 42 (the startup verification `fi`):**

```bash
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
```

**Key details:**
- Flock released at line 33, before the 2-second sleep (so `stop.sh` isn't blocked)
- All Docker operations are outside the flock
- `|| echo "WARNING..."` prevents `set -e` from killing the script on Docker failure
- Background pull uses `&>/dev/null &` so start.sh returns immediately
- `stat -c%s` is GNU coreutils (Linux/WSL2). On macOS, use `stat -f%z` instead.
- `EVENTS_FILE` resolved to absolute path (no tilde in Docker compose)

**File:** `start.sh`

### Phase 3: Extend `stop.sh`

Add Docker compose down **after** the receiver stop:

```bash
# --- Existing: stop receiver (SIGTERM/SIGKILL logic) ---
# ... (existing code, unchanged) ...

echo "Receiver stopped cleanly"

# --- Stop dashboard containers (if running) ---
if command -v docker &>/dev/null; then
    COMPOSE_FILE="${SCRIPT_DIR}/observability/docker-compose.yml"
    if [[ -f "$COMPOSE_FILE" ]] && docker compose -f "$COMPOSE_FILE" ps --quiet 2>/dev/null | grep -q .; then
        docker compose -f "$COMPOSE_FILE" down 2>&1
        echo "Dashboard stopped"
    fi
fi
```

**Key details:**
- Receiver stops first (prevents new events during teardown)
- Promtail positions persist to Docker volume; no data lost in the 2-3s gap
- Always checks for running containers regardless of sentinel file (prevents orphans)
- No `-p` flag needed -- `name: claude-agent` is in the compose file
- Worst-case `stop.sh` duration: 45s (receiver) + 10s (Docker) = ~55s

**File:** `stop.sh`

### Phase 4: Extend `status.sh`

Append dashboard status to the **same output line** as receiver status (preserves single-line machine-parseable contract):

```bash
# --- Existing receiver status check produces $status_line ---
# e.g., status_line="STATUS=healthy PID=1234"

# --- Append dashboard status ---
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
```

**Refactoring note:** The current `status.sh` echoes output directly (no variable). To append dashboard status on the same line, refactor the existing health checks to build a `status_line` string, then echo it once at the end. The existing exit code logic stays the same — set the exit code based on receiver health, then append dashboard info to the output string before the final echo.

**Key details:**
- Single output line (e.g., `STATUS=healthy PID=1234 DASHBOARD=running URL=http://localhost:3000`)
- Exit code reflects only receiver health (0=healthy, 1=unhealthy, 2=not_running)
- `TOTAL` derived dynamically from compose file (not hardcoded)
- Two dashboard states: `running` or `not_running`. Silence means disabled.
- No `-p` flag needed

**File:** `status.sh`

### Phase 5: Extend `setup.sh`

**Current file:** `setup.sh` (at repo root, ~319 lines)

**What to remove:** Lines 208-228 — the old `maybe_start_dashboard()` function that clones `claude-agent-dashboard` as a separate repo and runs its `start.sh`.

**What to add:** New `maybe_enable_dashboard()` function (insert at lines 208-228, replacing the old function).

**Where to call it:** In `main()`, between `register_repo_path` (line 301) and `maybe_start_receiver` (currently line 305). Replace `maybe_start_dashboard` call at line 304 with `maybe_enable_dashboard`.

**main() call order after changes:**
```
Line 258: detect_language
Line 259: detect_tailscale
Line 260: copy_templates
Line 261: save_config
Line 262: setup_secret
Line 301: register_repo_path
Line 304: maybe_enable_dashboard    # ← CHANGED (was maybe_start_dashboard)
Line 305: maybe_start_receiver      # ← unchanged
```

**New function (replaces lines 208-228):**

```bash
maybe_enable_dashboard() {
    local sentinel="${HOME}/.claude/agent-dashboard.enabled"

    # Skip if already configured (e.g., re-running setup for a second repo)
    if [[ -f "$sentinel" ]]; then
        ok "Dashboard already enabled"
        return
    fi

    if [[ ! -t 0 ]]; then
        # Non-interactive: default to disabled (no sentinel = disabled)
        return
    fi

    echo ""
    local answer
    read -rp "  Enable observability dashboard (Grafana + Prometheus)? [y/N] " answer || answer=""
    if [[ "${answer,,}" == "y" ]]; then
        if ! command -v docker &>/dev/null; then
            warn "Docker not found. Install it: https://docs.docker.com/get-docker/"
            echo "  After installing Docker, enable the dashboard by running:"
            echo "    touch ~/.claude/agent-dashboard.enabled"
            return
        fi
        touch "$sentinel"
        ok "Dashboard enabled. It will start with the receiver via start.sh."
    else
        echo "  Dashboard disabled. Enable later: touch ~/.claude/agent-dashboard.enabled"
    fi
}
```

**Key details:**
- Uses `ok` and `warn` helpers (already defined in setup.sh)
- Uses `${answer,,}` lowercase comparison (matches `maybe_start_receiver` pattern)
- Uses `read -rp` with `|| answer=""` pattern (matches existing functions)
- Non-interactive mode: returns silently (no sentinel = disabled, same as before setup)
- Re-run detection: if sentinel exists, skips prompt (dashboard is system-wide, not per-repo)

**File:** `setup.sh` (at repo root, NOT `scripts/setup.sh`)

### Phase 6: Update README.md

Update the README to:
- Remove references to `claude-agent-dashboard` as a separate repo
- Document the `observability/` directory and what it contains
- Update the Quick Start section to mention dashboard opt-in
- Document `~/.claude/agent-dashboard.enabled` sentinel file
- Add migration section for existing users (volume copy commands, stop old containers)
- Document JSONL rotation behavior

**File:** `README.md`

### Phase 7: Clean Up

- Remove `maybe_start_dashboard()` from `setup.sh` (the function that clones the separate repo)
- Ensure `observability/` is NOT in `.gitignore`
- Archive `claude-agent-dashboard` repo on GitHub (manual step, after 48h of stable operation)

## Acceptance Criteria

- [x] `observability/` directory contains all dashboard configs with modifications (compose name field, explicit volume names, Prometheus 90d retention, EVENTS_FILE fail-fast)
- [x] `start.sh` releases flock (`exec 200>&-`) before Docker section
- [x] `start.sh` rotates JSONL events file if >10 MiB
- [x] `start.sh` backgrounds Docker pull on first run (image not cached)
- [x] `start.sh` launches Docker stack when `~/.claude/agent-dashboard.enabled` exists
- [x] `start.sh` launches only receiver when sentinel absent
- [x] `start.sh` warns and continues if Docker not available but sentinel exists
- [x] `stop.sh` stops receiver first, then Docker containers
- [x] `stop.sh` stops Docker containers regardless of sentinel file (no orphans)
- [x] `status.sh` appends dashboard status to same output line as receiver status
- [x] `status.sh` derives container count dynamically (`docker compose config --services`)
- [x] `status.sh` exit code reflects only receiver health
- [x] `setup.sh` prompts for dashboard enablement (interactive mode)
- [x] `setup.sh` defaults to dashboard disabled in non-interactive mode
- [x] `setup.sh` skips prompt if sentinel already exists
- [x] No `source` of user-writable files anywhere in lifecycle scripts
- [x] `EVENTS_FILE` resolved to absolute path (no tilde)
- [x] Lifecycle scripts retain executable permissions (verify with `git ls-tree`)
- [x] Compose file uses `name: claude-agent` (no `-p` flag needed on invocations)
- [x] Compose volumes use explicit `name:` keys (project-name-independent)

## Dependencies & Risks

| Risk | Mitigation |
|------|-----------|
| Old dashboard containers cause port conflicts | Container names (`claude-grafana`, etc.) are the same; Docker errors are self-descriptive. README documents how to stop old stack. |
| Old volumes not automatically migrated | README documents one-time volume copy command. Data loss is acceptable for a dev tool if user skips migration. |
| Dashboard JSON files add ~3000 lines to repo | Machine-generated, low churn, necessary for provisioning |
| First-run image pull (~1.7GB) takes minutes | Backgrounded in start.sh with progress message |
| JSONL rotation loses events older than Loki retention | Rotation threshold (10 MiB) covers ~27 days at current rate; Loki retains 7 days. No data gap. |

## Sources & References

### Origin

- **Brainstorm document:** [docs/brainstorms/2026-03-26-merge-dashboard-into-bootstrap-brainstorm.md](docs/brainstorms/2026-03-26-merge-dashboard-into-bootstrap-brainstorm.md) -- Key decisions carried forward: `observability/` directory, single-unit lifecycle, interactive prompt activation, copy & adapt migration.

### Internal References

- Lifecycle brainstorm (receiver cannot be containerized): `docs/brainstorms/2026-03-19-receiver-lifecycle-brainstorm.md`
- MCP permissions gotcha: `docs/plans/2026-03-19-002-fix-mcp-push-file-permissions-plan.md`
- Refire visibility dashboard panels (Phase 3 incomplete): `docs/plans/2026-03-24-001-feat-agent-refire-visibility-dashboard-plan.md`
- Worker target repo context (setup.sh evolution): `docs/plans/2026-03-26-001-feat-worker-target-repo-context-plan.md`

### External References

- Docker Docs: [Specify a project name](https://docs.docker.com/compose/how-tos/project-name/) -- `name:` field in compose file
- Docker Docs: [Version and name top-level elements](https://docs.docker.com/reference/compose-file/version-and-name/)
- Docker Install: [github.com/docker/docker-install](https://github.com/docker/docker-install) -- official convenience script
- Bash Config Security: [Bash Hackers Wiki: Config files](https://flokoe.github.io/bash-hackers-wiki/howto/conffile/) -- why not to `source` user-writable files

### Divergences from Brainstorm

| Brainstorm Decision | Plan Adjustment | Reason |
|---------------------|-----------------|--------|
| Config in `.claude/bootstrap.conf` | Sentinel file `~/.claude/agent-dashboard.enabled` | bootstrap.conf is per-target-repo; dashboard is global. Sentinel eliminates `source` security risk. |
| Scripts move to `scripts/` | Scripts stay at root | Breaking change, `SCRIPT_DIR` path resolution, not worth the churn |
| Docker auto-install everywhere | Check-and-link (print URL) | `curl \| sudo sh` is HIGH severity security risk; two of three platform paths just print URLs anyway |

