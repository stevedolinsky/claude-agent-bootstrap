# Configuration Reference

The receiver loads configuration from a TOML file at `~/.claude/agent-receiver.toml`. All fields are optional — defaults are used for any omitted field. CLI flags take precedence over TOML values.

## CLI Flags

| Flag | Description | Default |
|------|-------------|---------|
| `-c, --config PATH` | Path to TOML config file | `~/.claude/agent-receiver.toml` |
| `-p, --port PORT` | Override listening port | From config or `9876` |
| `-v, --verbose` | Enable debug logging | Off |

## TOML Fields

### Server

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `port` | int | `9876` | HTTP server listening port |
| `bind_address` | string | `"0.0.0.0"` | Address to bind to. Use your Tailscale IP for security-restricted deployments. |

### Paths

All paths support `~` expansion.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `queue_dir` | string | `"~/.claude/queues"` | Directory for persistent queue files (one JSON file per repo) |
| `events_file` | string | `"~/.claude/agent-events.jsonl"` | Structured JSONL event log |
| `secret_file` | string | `"~/.claude/agent-webhook.secret"` | HMAC shared secret file (chmod 600) |
| `budget_file` | string | `"~/.claude/agent-budget.json"` | Daily budget state (resets at midnight UTC) |
| `plans_dir` | string | `"~/.claude/plans"` | Epic plan files (`epic-<number>.json`) |
| `workers_dir` | string | `"~/.claude/workers"` | Worker PID files for process management |

### Timeouts

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `worker_timeout_simple` | int | `1800` | Timeout in seconds for simple tasks (30 minutes) |
| `worker_timeout_step` | int | `900` | Timeout in seconds for epic sub-task steps (15 minutes) |
| `heartbeat_interval` | int | `30` | Seconds between heartbeat event emissions |

### Budget

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `daily_budget_usd` | float | `50.0` | Maximum estimated daily spend. When exceeded, all dispatching pauses until midnight UTC. |
| `per_worker_budget_usd` | float | `5.0` | Per-worker budget passed to `claude --print --max-budget-usd`. |

### Safety

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `circuit_breaker_max` | int | `3` | Maximum responses per PR within the circuit breaker window |
| `circuit_breaker_window` | int | `600` | Circuit breaker window in seconds (10 minutes) |
| `max_retries` | int | `3` | Maximum worker retry attempts before marking `agent-blocked` |

### Reserved (defined but not yet active)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `reconciliation_interval` | int | `1800` | Planned: interval for catch-up reconciliation polling. Currently defined in Config but not referenced by any code. |

## Complete Example

```toml
# ~/.claude/agent-receiver.toml

# Server
port = 9876
bind_address = "100.64.1.23"  # Tailscale IP for restricted access

# Paths (all support ~ expansion)
queue_dir = "~/.claude/queues"
events_file = "~/.claude/agent-events.jsonl"
secret_file = "~/.claude/agent-webhook.secret"
budget_file = "~/.claude/agent-budget.json"
plans_dir = "~/.claude/plans"
workers_dir = "~/.claude/workers"

# Timeouts
worker_timeout_simple = 1800  # 30 minutes
worker_timeout_step = 900     # 15 minutes per epic step
heartbeat_interval = 30

# Budget
daily_budget_usd = 50.0
per_worker_budget_usd = 5.0

# Safety
circuit_breaker_max = 3
circuit_breaker_window = 600  # 10 minutes
max_retries = 3
```

## Precedence

1. CLI flags (highest)
2. TOML config file
3. Built-in defaults (lowest)

Currently only `port` can be overridden via CLI flag. All other fields use TOML or defaults.
