"""Prometheus metrics for the agent webhook receiver.

Exposes counters, gauges, and histograms that Prometheus scrapes via /metrics.
Counter state is persisted to disk so cumulative totals survive process restarts.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import tempfile
import threading

from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric Definitions
# ---------------------------------------------------------------------------

COST_TOTAL = Counter(
    "agent_cost_usd_total",
    "Cumulative API cost in USD",
    ["repo", "model"],
)

TOKENS_TOTAL = Counter(
    "agent_tokens_total",
    "Total tokens consumed",
    ["repo", "model", "type"],
)

ISSUES_TOTAL = Counter(
    "agent_issues_total",
    "Total issues processed by lifecycle stage",
    ["repo", "action", "reason"],
)

QUEUE_DEPTH = Gauge(
    "agent_queue_depth",
    "Current queue depth",
    ["repo"],
)

IN_FLIGHT = Gauge(
    "agent_in_flight",
    "Currently running workers",
    ["repo"],
)

WORKER_DURATION = Histogram(
    "agent_worker_duration_seconds",
    "Worker execution time in seconds",
    ["repo", "model"],
    buckets=[30, 60, 120, 180, 300, 600, 900, 1200, 1800],
)

# ---------------------------------------------------------------------------
# State Persistence
# ---------------------------------------------------------------------------

STATE_FILE = os.path.expanduser("~/.claude/agent-metrics-state.json")
_save_lock = threading.Lock()


def _atomic_write_json(filepath: str, data: dict) -> None:
    """Write JSON atomically using temp file + rename."""
    dir_path = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_state() -> None:
    """Save all counter values to disk atomically."""
    with _save_lock:
        state: dict[str, dict[str, float]] = {}
        for metric_name, metric_obj in [
            ("agent_cost_usd_total", COST_TOTAL),
            ("agent_tokens_total", TOKENS_TOTAL),
            ("agent_issues_total", ISSUES_TOTAL),
        ]:
            data = {}
            for labels, m in metric_obj._metrics.items():
                key = "|".join(str(label) for label in labels)
                data[key] = m._value.get()
            state[metric_name] = data
        _atomic_write_json(STATE_FILE, state)


def load_state() -> None:
    """Restore counter values from disk on startup."""
    if not os.path.exists(STATE_FILE):
        log.info("No metrics state file found at %s — starting fresh", STATE_FILE)
        return
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log.warning("Could not load metrics state from %s: %s", STATE_FILE, e)
        return

    for key, val in state.get("agent_cost_usd_total", {}).items():
        parts = key.split("|")
        if len(parts) == 2 and val > 0:
            COST_TOTAL.labels(repo=parts[0], model=parts[1]).inc(val)

    for key, val in state.get("agent_tokens_total", {}).items():
        parts = key.split("|")
        if len(parts) == 3 and val > 0:
            TOKENS_TOTAL.labels(repo=parts[0], model=parts[1], type=parts[2]).inc(val)

    for key, val in state.get("agent_issues_total", {}).items():
        parts = key.split("|")
        if len(parts) == 3 and val > 0:
            ISSUES_TOTAL.labels(repo=parts[0], action=parts[1], reason=parts[2]).inc(val)

    log.info("Restored metrics state from %s", STATE_FILE)


def setup_persistence() -> None:
    """Register shutdown hooks to save state on exit."""
    atexit.register(save_state)

    def _handle_sigterm(signum: int, frame: object) -> None:
        save_state()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)
