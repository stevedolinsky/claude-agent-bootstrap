"""Sequential worker dispatcher with cost tracking and epic continuation."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .exceptions import BudgetExhaustedError, WorkerSpawnError, WorkerTimeoutError
from . import metrics as prom
from .queue import QueueItem, WorkQueue
from .server import Config, LABELS, record_circuit_breaker

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event Logger
# ---------------------------------------------------------------------------

VALID_ACTIONS = frozenset({
    "received",
    "skipped",
    "spawned",
    "done",
    "error",
    "heartbeat",
    "queue_added",
    "dispatched",
    "plan_created",
    "step_started",
    "step_completed",
    "pr_created",
    "blocked",
    "cost_tracked",
    "budget_exhausted",
    "triage",
})


class EventLogger:
    """Append-only JSONL event logger. Thread-safe, line-buffered."""

    def __init__(self, jsonl_path: Path, repo_default: str | None = None) -> None:
        self._path = jsonl_path
        self._repo_default = repo_default
        self._lock = threading.Lock()
        self._file = open(jsonl_path, "a")  # noqa: SIM115

    def log(self, action: str, **kwargs: Any) -> None:
        """Append one event to JSONL. Strips None values."""
        if action not in VALID_ACTIONS:
            log.warning("Unknown event action: %s", action)

        event: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "action": action,
        }
        if self._repo_default and "repo" not in kwargs:
            event["repo"] = self._repo_default
        event.update({k: v for k, v in kwargs.items() if v is not None})

        line = json.dumps(event, default=str) + "\n"
        with self._lock:
            self._file.write(line)
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()


# ---------------------------------------------------------------------------
# Worker Result + Cost Estimation
# ---------------------------------------------------------------------------

# Published API pricing per million tokens (configurable)
API_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_create": 3.75,
    },
    "claude-opus-4-6": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.50,
        "cache_create": 18.75,
    },
}


@dataclass(slots=True)
class WorkerResult:
    """Parsed output from a claude --print --output-format json invocation."""

    exit_code: int
    output: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0  # total_cost_usd from CLI (0.0 on OAuth)
    estimated_api_cost_usd: float = 0.0
    duration_ms: int = 0
    model: str = ""


def parse_worker_output(stdout: str, exit_code: int) -> WorkerResult:
    """Parse JSON output from claude --print --output-format json."""
    result = WorkerResult(exit_code=exit_code)

    if not stdout.strip():
        return result

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # If not JSON, treat as plain text output
        result.output = stdout
        return result

    result.output = data.get("result", stdout)
    result.cost_usd = data.get("total_cost_usd", 0.0)
    result.duration_ms = data.get("duration_ms", 0)

    usage = data.get("usage", {})
    result.input_tokens = usage.get("input_tokens", 0)
    result.output_tokens = usage.get("output_tokens", 0)
    result.cache_read_tokens = usage.get("cache_read_input_tokens", 0)
    result.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)

    # Extract model from modelUsage keys
    model_usage = data.get("modelUsage", {})
    if model_usage:
        raw_model = next(iter(model_usage))
        # Normalize: "claude-sonnet-4-6[1m]" -> "claude-sonnet-4-6"
        result.model = raw_model.split("[")[0]

    result.estimated_api_cost_usd = estimate_api_cost(result)
    return result


def estimate_api_cost(result: WorkerResult) -> float:
    """Compute estimated API-equivalent cost from token counts."""
    model_key = result.model
    # Try exact match, then prefix match, then default to sonnet
    rates = API_PRICING.get(model_key)
    if not rates:
        for key in API_PRICING:
            if model_key.startswith(key.rsplit("-", 1)[0]):
                rates = API_PRICING[key]
                break
    if not rates:
        rates = API_PRICING["claude-sonnet-4-6"]

    return (
        result.input_tokens * rates["input"] / 1_000_000
        + result.output_tokens * rates["output"] / 1_000_000
        + result.cache_read_tokens * rates["cache_read"] / 1_000_000
        + result.cache_creation_tokens * rates["cache_create"] / 1_000_000
    )


# ---------------------------------------------------------------------------
# Budget Tracker
# ---------------------------------------------------------------------------

@dataclass
class DailyBudget:
    """Tracks cumulative daily spend. Persisted to disk."""

    date: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    estimated_cost_usd: float = 0.0
    worker_count: int = 0

    def reset_if_new_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.date != today:
            self.date = today
            self.total_input_tokens = 0
            self.total_output_tokens = 0
            self.total_cache_read_tokens = 0
            self.total_cache_creation_tokens = 0
            self.estimated_cost_usd = 0.0
            self.worker_count = 0

    def add(self, result: WorkerResult) -> None:
        self.total_input_tokens += result.input_tokens
        self.total_output_tokens += result.output_tokens
        self.total_cache_read_tokens += result.cache_read_tokens
        self.total_cache_creation_tokens += result.cache_creation_tokens
        self.estimated_cost_usd += result.estimated_api_cost_usd
        self.worker_count += 1


def load_budget(path: Path) -> DailyBudget:
    """Load budget from disk or create fresh."""
    budget = DailyBudget()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            budget = DailyBudget(**data)
        except (json.JSONDecodeError, TypeError):
            log.warning("Corrupt budget file %s, starting fresh", path)
    budget.reset_if_new_day()
    return budget


def save_budget(path: Path, budget: DailyBudget) -> None:
    """Atomic write budget to disk."""
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(budget), f)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError:
        log.exception("Failed to save budget to %s", path)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class Dispatcher:
    """Sequential work queue dispatcher. One worker per repo at a time."""

    def __init__(
        self,
        queue: WorkQueue,
        events: EventLogger,
        config: Config,
    ) -> None:
        self._queue = queue
        self._events = events
        self._config = config
        self._shutdown = threading.Event()
        self._budget_exhausted = False

        self._repo_threads: dict[str, threading.Thread] = {}
        self._budget = load_budget(config.budget_file)
        self._budget_lock = threading.Lock()

    def ensure_repo_loop(self, repo: str) -> None:
        """Start a dispatch loop for repo if not already running."""
        if repo in self._repo_threads and self._repo_threads[repo].is_alive():
            return
        t = threading.Thread(
            target=self._dispatch_loop,
            args=(repo,),
            name=f"dispatch-{repo}",
            daemon=True,
        )
        self._repo_threads[repo] = t
        t.start()
        log.info("Started dispatch loop for %s", repo)

    def notify(self, repo: str) -> None:
        """Signal the repo's dispatch loop that new work is available."""
        self._queue.wait_for_work(repo, timeout=0)  # Just to ensure event exists
        # The enqueue already signals the event via queue.enqueue()

    def stop(self, timeout: float = 30.0) -> None:
        """Signal all loops to exit and join threads."""
        self._shutdown.set()
        # Wake all dispatch loops so they see the shutdown flag immediately
        for repo in list(self._queue.repos()) + list(self._repo_threads.keys()):
            self._queue.wait_for_work(repo, timeout=0)  # Ensure event exists
            self._queue._events.get(repo, threading.Event()).set()
        for repo, thread in self._repo_threads.items():
            log.info("Waiting for dispatch loop %s to exit...", repo)
            thread.join(timeout=timeout / max(len(self._repo_threads), 1))
            if thread.is_alive():
                log.warning("Dispatch loop %s did not exit cleanly", repo)

    def start_heartbeat(self) -> threading.Thread:
        """Start heartbeat emission thread."""
        t = threading.Thread(
            target=self._heartbeat_loop,
            name="heartbeat",
            daemon=True,
        )
        t.start()
        return t

    # --- Dispatch loop ---

    def _dispatch_loop(self, repo: str) -> None:
        """Per-repo loop: wait for signal, pop item, spawn worker, repeat."""
        log.info("Dispatch loop started for %s", repo)

        while not self._shutdown.is_set():
            # Wait for work or shutdown
            self._queue.wait_for_work(repo, timeout=10.0)

            if self._shutdown.is_set():
                break

            if self._budget_exhausted:
                log.warning("Budget exhausted, dispatch paused for %s", repo)
                self._shutdown.wait(60)  # Interruptible sleep
                continue

            item = self._queue.take_next(repo)
            if item is None:
                continue

            log.info(
                "Dispatching %s #%d for %s (attempt %d)",
                item.type,
                item.number,
                repo,
                item.attempts + 1,
            )

            # Select model via LLM triage
            model = self._select_model(item)

            self._events.log(
                "triage",
                repo=repo,
                number=item.number,
                model=model,
                complexity="complex" if "opus" in model else "simple",
            )

            self._events.log(
                "dispatched",
                repo=repo,
                number=item.number,
                model=model,
            )

            try:
                result = self._run_worker(repo, item, model)

                # Track cost
                self._track_cost(repo, item, result)

                if result.exit_code == 0:
                    # Record circuit breaker for comment types
                    if item.type in ("pr_comment", "issue_comment"):
                        record_circuit_breaker(repo, item.type, item.number)

                    # Check for epic continuation
                    self._handle_epic_continuation(repo, item)
                    self._queue.complete(repo, item.dedup_key)

                    self._events.log(
                        "done",
                        repo=repo,
                        number=item.number,
                        model=model,
                        duration_seconds=result.duration_ms / 1000,
                    )
                    prom.ISSUES_TOTAL.labels(repo=repo, action="done", reason="").inc()
                    prom.IN_FLIGHT.labels(repo=repo).dec()
                    prom.QUEUE_DEPTH.labels(repo=repo).dec()
                    prom.WORKER_DURATION.labels(repo=repo, model=model).observe(
                        result.duration_ms / 1000
                    )
                else:
                    self._handle_failure(repo, item, result, model)

            except WorkerTimeoutError:
                self._events.log(
                    "blocked",
                    repo=repo,
                    number=item.number,
                    block_reason="worker_timeout",
                )
                prom.ISSUES_TOTAL.labels(repo=repo, action="blocked", reason="worker_timeout").inc()
                prom.IN_FLIGHT.labels(repo=repo).dec()
                prom.QUEUE_DEPTH.labels(repo=repo).dec()
                self._queue.complete(repo, item.dedup_key)
                log.error("Worker timed out for %s #%d", repo, item.number)

            except WorkerSpawnError as exc:
                self._events.log(
                    "error",
                    repo=repo,
                    number=item.number,
                    detail=str(exc),
                )
                prom.ISSUES_TOTAL.labels(repo=repo, action="error", reason=str(exc)[:50]).inc()
                prom.IN_FLIGHT.labels(repo=repo).dec()
                prom.QUEUE_DEPTH.labels(repo=repo).dec()
                self._queue.complete(repo, item.dedup_key)

            except BudgetExhaustedError:
                log.warning("Budget exhausted after %s #%d", repo, item.number)
                self._queue.complete(repo, item.dedup_key)
                # Don't break — let the loop check _budget_exhausted flag

        log.info("Dispatch loop exiting for %s", repo)

    def _select_model(self, item: QueueItem) -> str:
        """Select model via LLM triage. Sonnet analyzes complexity, routes to Opus if needed."""
        # PR comments and maintenance always use Sonnet (fast/cheap)
        if item.type in ("pr_comment", "issue_comment", "maintenance"):
            return "claude-sonnet-4-6"

        # No content to analyze → default Sonnet
        if not item.title and not item.body:
            return "claude-sonnet-4-6"

        return self._triage_issue(item)

    def _triage_issue(self, item: QueueItem) -> str:
        """Spawn a fast Sonnet call to classify issue complexity."""
        triage_prompt = (
            "You are a complexity classifier for GitHub issues. "
            "Given the issue title and body, classify as SIMPLE or COMPLEX.\n\n"
            "SIMPLE: bug fix, small feature, config change, docs update, style fix, "
            "test addition, single-file change, clear implementation path.\n"
            "COMPLEX: architecture change, multi-file refactor, security audit, "
            "system design, migration, performance optimization, new subsystem, "
            "cross-cutting concern, ambiguous requirements needing exploration.\n\n"
            f"Issue title: {item.title}\n"
            f"Issue body: {item.body[:2000]}\n\n"
            "Reply with exactly one word: SIMPLE or COMPLEX"
        )

        try:
            proc = subprocess.run(
                [
                    "claude",
                    "--print",
                    "--model", "claude-sonnet-4-6",
                    "--max-turns", "1",
                ],
                input=triage_prompt,
                capture_output=True,
                text=True,
                timeout=30,
            )

            response = proc.stdout.strip().upper()
            # Extract the classification word from the response
            if "COMPLEX" in response:
                model = "claude-opus-4-6"
                log.info(
                    "Triage: #%d classified as COMPLEX → opus",
                    item.number,
                )
            else:
                model = "claude-sonnet-4-6"
                log.info(
                    "Triage: #%d classified as SIMPLE → sonnet",
                    item.number,
                )

            return model

        except subprocess.TimeoutExpired:
            log.warning("Triage timed out for #%d, defaulting to sonnet", item.number)
            return "claude-sonnet-4-6"
        except Exception:
            log.exception("Triage failed for #%d, defaulting to sonnet", item.number)
            return "claude-sonnet-4-6"

    def _run_worker(self, repo: str, item: QueueItem, model: str) -> WorkerResult:
        """Spawn worker subprocess and wait for completion."""
        # Build prompt (placeholder — real prompts come from templates)
        prompt = self._build_prompt(repo, item)

        timeout = (
            self._config.worker_timeout_step
            if item.type == "maintenance"
            else self._config.worker_timeout_simple
        )

        self._events.log(
            "spawned",
            repo=repo,
            number=item.number,
            model=model,
        )
        prom.ISSUES_TOTAL.labels(repo=repo, action="spawned", reason="").inc()
        prom.IN_FLIGHT.labels(repo=repo).inc()

        # Write prompt to temp file for stdin
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, prefix="agent-prompt-"
        ) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            cmd = [
                "claude",
                "--print",
                "--output-format", "json",
                "--model", model,
                "--max-budget-usd", str(self._config.per_worker_budget_usd),
            ]

            proc = subprocess.Popen(
                cmd,
                stdin=open(prompt_file),  # noqa: SIM115
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
            )

            # Write PID file
            pid_file = self._config.workers_dir / f"{repo.replace('/', '-')}.pid"
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(proc.pid))

            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Two-phase termination
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    proc.wait(timeout=10)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        proc.wait()
                    except ProcessLookupError:
                        pass
                raise WorkerTimeoutError(
                    f"Worker for {repo} #{item.number} timed out after {timeout}s"
                )
            finally:
                pid_file.unlink(missing_ok=True)

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            return parse_worker_output(stdout, proc.returncode)

        finally:
            os.unlink(prompt_file)

    def _build_prompt(self, repo: str, item: QueueItem) -> str:
        """Build the prompt for a worker. Placeholder for template system."""
        return (
            f"You are working on {repo}.\n"
            f"Task type: {item.type}\n"
            f"Issue/PR number: {item.number}\n"
        )

    def _handle_failure(
        self, repo: str, item: QueueItem, result: WorkerResult, model: str
    ) -> None:
        """Handle worker failure — retry or block."""
        item.attempts += 1
        if item.attempts < self._config.max_retries:
            log.warning(
                "Worker failed for %s #%d (attempt %d/%d), re-queuing",
                repo,
                item.number,
                item.attempts,
                self._config.max_retries,
            )
            self._queue.complete(repo, item.dedup_key)
            self._queue.enqueue(repo, item)
        else:
            log.error(
                "Worker failed for %s #%d after %d attempts, marking blocked",
                repo,
                item.number,
                item.attempts,
            )
            self._events.log(
                "blocked",
                repo=repo,
                number=item.number,
                block_reason=f"failed_after_{item.attempts}_attempts",
            )
            self._queue.complete(repo, item.dedup_key)

    def _handle_epic_continuation(self, repo: str, item: QueueItem) -> None:
        """Check plan file for remaining steps. Re-enqueue if more work."""
        plan_file = self._config.plans_dir / f"epic-{item.number}.json"
        if not plan_file.exists():
            return

        try:
            plan = json.loads(plan_file.read_text())
            steps = plan.get("steps", [])

            # Find first pending step
            next_step = None
            for step in steps:
                if step.get("status") == "pending":
                    next_step = step
                    break

            if next_step is not None:
                # Re-enqueue for next step
                continuation = QueueItem(
                    type="issue",
                    number=item.number,
                    queued_at=QueueItem.now_iso(),
                )
                self._queue.requeue_front(repo, continuation)
                log.info(
                    "Epic #%d: continuing to step '%s'",
                    item.number,
                    next_step.get("name", "unknown"),
                )
        except (json.JSONDecodeError, OSError):
            log.exception("Failed to read plan file for epic #%d", item.number)

    # --- Cost tracking ---

    def _track_cost(
        self, repo: str, item: QueueItem, result: WorkerResult
    ) -> None:
        """Track cumulative cost and check daily budget."""
        with self._budget_lock:
            self._budget.reset_if_new_day()
            self._budget.add(result)
            save_budget(self._config.budget_file, self._budget)

            self._events.log(
                "cost_tracked",
                repo=repo,
                number=item.number,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cache_creation_tokens=result.cache_creation_tokens,
                estimated_cost_usd=round(result.estimated_api_cost_usd, 6),
                daily_cumulative_usd=round(self._budget.estimated_cost_usd, 6),
            )

            # Prometheus metrics
            prom.COST_TOTAL.labels(repo=repo, model=result.model).inc(
                result.estimated_api_cost_usd
            )
            for token_type, count in [
                ("input", result.input_tokens),
                ("output", result.output_tokens),
                ("cache_read", result.cache_read_tokens),
                ("cache_create", result.cache_creation_tokens),
            ]:
                if count:
                    prom.TOKENS_TOTAL.labels(repo=repo, model=result.model, type=token_type).inc(count)
            prom.save_state()

            if self._budget.estimated_cost_usd >= self._config.daily_budget_usd:
                self._budget_exhausted = True
                self._events.log(
                    "budget_exhausted",
                    daily_cumulative_usd=round(self._budget.estimated_cost_usd, 6),
                    daily_budget_usd=self._config.daily_budget_usd,
                )
                log.warning(
                    "BUDGET EXHAUSTED: $%.2f >= $%.2f daily limit",
                    self._budget.estimated_cost_usd,
                    self._config.daily_budget_usd,
                )

    # --- Heartbeat ---

    def _heartbeat_loop(self) -> None:
        """Emit heartbeat events every config.heartbeat_interval seconds."""
        while not self._shutdown.is_set():
            for repo in self._queue.repos():
                with self._budget_lock:
                    daily_cost = self._budget.estimated_cost_usd
                    daily_budget = self._config.daily_budget_usd
                    daily_workers = self._budget.worker_count

                self._events.log(
                    "heartbeat",
                    repo=repo,
                    model="receiver",
                    event_type="alive",
                    queue_depth=self._queue.get_depth(repo),
                    daily_cost_usd=round(daily_cost, 4),
                    daily_budget_usd=daily_budget,
                    daily_workers=daily_workers,
                )

            # Persist Prometheus counter state alongside heartbeat
            try:
                prom.save_state()
            except Exception:
                log.exception("Failed to save metrics state")

            self._shutdown.wait(self._config.heartbeat_interval)
