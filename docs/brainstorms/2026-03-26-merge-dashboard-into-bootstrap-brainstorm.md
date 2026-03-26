# Brainstorm: Merge Dashboard into Bootstrap

**Date:** 2026-03-26
**Status:** Draft

## What We're Building

Merge the `claude-agent-dashboard` repo into `claude-agent-bootstrap` as an `observability/` subdirectory. The dashboard (Grafana + Loki + Promtail + Prometheus + Node Exporter) becomes an optional component activated during first-run setup. The lifecycle scripts (`start.sh`, `stop.sh`, `status.sh`) become a unified control plane for both the receiver and observability stack.

### Goals
- Single repo to clone, set up, and manage
- One `start` / `stop` / `status` for everything
- Dashboard is opt-in (not required to run the receiver)
- Docker auto-installed if user enables dashboard and Docker is missing

## Why This Approach

The dashboard repo has no independent value — it only exists to visualize bootstrap's JSONL events and Prometheus metrics. Keeping it separate means:
- Two repos to clone and keep in sync
- Two sets of start/stop scripts to remember
- Setup.sh already has an awkward "clone the other repo" prompt

Merging simplifies the mental model: **one system, one repo, one lifecycle.**

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Directory layout** | `observability/` subdirectory | Descriptive name, clean separation from receiver code |
| **Lifecycle model** | Single unit | `start.sh` launches receiver + dashboard (if enabled). `stop.sh` kills both. Simplest mental model. |
| **Dashboard activation** | Interactive prompt during `setup.sh` | Asks "Enable observability dashboard?" and persists choice |
| **Config persistence** | `DASHBOARD_ENABLED=true` in `.claude/bootstrap.conf` | Reuses existing setup config file, no new files |
| **Missing Docker handling** | Auto-install Docker | If user enables dashboard but Docker isn't present, offer to install it |
| **Migration strategy** | Copy & Adapt | Copy files into `observability/`, rewrite scripts, delete old repo. Dashboard git history is not worth preserving. |

## Proposed Structure

```
claude-agent-bootstrap/
  receiver/                  # (unchanged)
  observability/
    docker-compose.yml       # Copied from dashboard repo, paths adjusted
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
  scripts/
    setup.sh                 # Gains: Docker install, dashboard prompt
    start.sh                 # Gains: launch Docker stack if DASHBOARD_ENABLED
    stop.sh                  # Gains: stop Docker stack if running
    status.sh                # Gains: report dashboard container health
  templates/                 # (unchanged)
  tests/                     # (unchanged)
```

## Lifecycle Behavior

### `setup.sh` (first run in a target repo)
1. Existing steps (language detection, Tailscale, templates, webhook secret, etc.)
2. **New:** "Enable observability dashboard? (y/n)"
3. If yes:
   - Check for Docker. If missing, auto-install (`apt-get` / `brew` / etc.)
   - Write `DASHBOARD_ENABLED=true` to `.claude/bootstrap.conf`
4. If no:
   - Write `DASHBOARD_ENABLED=false` to `.claude/bootstrap.conf`
5. Existing: start receiver prompt

### `start.sh`
1. Start receiver (existing PID/flock logic)
2. Read `DASHBOARD_ENABLED` from bootstrap.conf
3. If enabled: `docker compose -f observability/docker-compose.yml up -d`
4. Report both statuses

### `stop.sh`
1. Stop receiver (existing SIGTERM/SIGKILL logic)
2. If dashboard containers running: `docker compose -f observability/docker-compose.yml down`

### `status.sh`
1. Receiver status (existing health check)
2. If dashboard enabled: report Grafana/Loki/Prometheus container status
3. Output URL: `Dashboard: http://localhost:3000`

## Resolved Questions

- **What about the dashboard repo after merge?** Archive it on GitHub. It served its purpose.
- **Will docker-compose paths break?** The compose file uses relative paths and env vars (`$EVENTS_FILE`). Moving it to `observability/` just means the `-f` flag points there. Volume mounts for the JSONL file use absolute paths (`~/.claude/agent-events.jsonl`), so no change needed.

## Open Questions

None — all questions resolved during brainstorming.
