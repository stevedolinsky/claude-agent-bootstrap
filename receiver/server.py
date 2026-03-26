"""HTTP webhook server with Config, Guards, and HMAC authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from datetime import datetime, timezone

from .exceptions import WebhookAuthError
from . import metrics as prom
from .metrics import generate_latest, CONTENT_TYPE_LATEST
from .queue import QueueItem

if TYPE_CHECKING:
    from .dispatcher import Dispatcher
    from .queue import WorkQueue

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LABELS = {
    "ready": "agent",
    "wip": "agent-wip",
    "blocked": "agent-blocked",
}


@dataclass(frozen=True, slots=True)
class Config:
    """Receiver configuration. Loaded from TOML or defaults."""

    port: int = 9876
    bind_address: str = "0.0.0.0"
    queue_dir: Path = field(default_factory=lambda: Path("~/.claude/queues"))
    events_file: Path = field(default_factory=lambda: Path("~/.claude/agent-events.jsonl"))
    secret_file: Path = field(default_factory=lambda: Path("~/.claude/agent-webhook.secret"))
    budget_file: Path = field(default_factory=lambda: Path("~/.claude/agent-budget.json"))
    plans_dir: Path = field(default_factory=lambda: Path("~/.claude/plans"))
    workers_dir: Path = field(default_factory=lambda: Path("~/.claude/workers"))
    heartbeat_interval: int = 30
    worker_timeout_simple: int = 1800  # 30 minutes
    worker_timeout_step: int = 900  # 15 minutes per epic sub-task
    circuit_breaker_max: int = 3
    circuit_breaker_window: int = 600  # seconds
    max_retries: int = 3
    daily_budget_usd: float = 50.0
    per_worker_budget_usd: float = 5.0
    reconciliation_interval: int = 1800  # 30 minutes
    repo_paths: dict[str, str] = field(default_factory=dict)  # repo -> local path

    def __post_init__(self) -> None:
        # Expand ~ in all Path fields
        for f in (
            "queue_dir",
            "events_file",
            "secret_file",
            "budget_file",
            "plans_dir",
            "workers_dir",
        ):
            val = getattr(self, f)
            if isinstance(val, Path):
                object.__setattr__(self, f, val.expanduser())

        # Load repo paths from JSON sidecar
        repos_file = Path("~/.claude/agent-repos.json").expanduser()
        if repos_file.exists() and not self.repo_paths:
            try:
                data = json.loads(repos_file.read_text())
                object.__setattr__(self, "repo_paths", data)
            except (json.JSONDecodeError, OSError):
                pass

    @classmethod
    def from_file(cls, path: Path) -> Config:
        """Load config from TOML file."""
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            try:
                import tomllib  # type: ignore[import]
            except ImportError:
                import tomli as tomllib  # type: ignore[import,no-redef]

        with open(path, "rb") as f:
            data = tomllib.load(f)

        # Convert string paths to Path objects
        for key in ("queue_dir", "events_file", "secret_file", "budget_file",
                     "plans_dir", "workers_dir"):
            if key in data and isinstance(data[key], str):
                data[key] = Path(data[key])

        return cls(**data)

    def ensure_dirs(self) -> None:
        """Create required directories with secure permissions."""
        for d in (self.queue_dir, self.plans_dir, self.workers_dir):
            d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass
        # Ensure events file parent exists
        self.events_file.parent.mkdir(parents=True, exist_ok=True)

    def validate_permissions(self) -> None:
        """Warn if secret file has overly permissive permissions."""
        if self.secret_file.exists():
            mode = self.secret_file.stat().st_mode & 0o777
            if mode & 0o077:
                log.warning(
                    "Secret file %s has mode %o — should be 0600",
                    self.secret_file,
                    mode,
                )


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GuardResult:
    """Result of a single guard check."""

    name: str      # e.g., "self_reply_marker"
    result: str    # "pass" or "fail"
    detail: str    # human-readable, never includes user-generated content


def check_self_reply(comment_body: str) -> str | GuardResult:
    """Returns skip reason (str) if self-reply detected, GuardResult on pass."""
    if not comment_body:
        return GuardResult("self_reply", "pass", "empty body")
    # Layer 1: HTML marker
    if "<!-- claude-agent -->" in comment_body:
        return "self_reply_marker"
    # Layer 2: Visible signature
    for sig in ("· claude-sonnet-", "· claude-opus-", "· claude-haiku-"):
        if sig in comment_body:
            return "self_reply_signature"
    return GuardResult("self_reply", "pass", "no marker or signature found")


# Module-level state for circuit breaker (in-memory, not persisted)
_circuit_breaker_state: dict[str, list[float]] = {}
_circuit_breaker_lock = threading.Lock()


def check_circuit_breaker(
    repo: str, item_type: str, number: int, max_responses: int = 3, window: int = 600
) -> str | GuardResult:
    """Returns skip reason (str) if limit exceeded, GuardResult on pass."""
    import time

    key = f"{repo}#{item_type}#{number}"
    now = time.time()

    with _circuit_breaker_lock:
        timestamps = _circuit_breaker_state.get(key, [])
        # Prune expired entries
        timestamps = [t for t in timestamps if now - t < window]
        _circuit_breaker_state[key] = timestamps

        if len(timestamps) >= max_responses:
            return f"circuit_breaker_{len(timestamps)}_in_{window}s"

        count = len(timestamps)
        if timestamps:
            last_iso = datetime.fromtimestamp(timestamps[-1], tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            detail = f"count={count}, last_seen={last_iso}"
        else:
            detail = f"count={count}, no_prior_responses"
        return GuardResult("circuit_breaker", "pass", detail)


def record_circuit_breaker(repo: str, item_type: str, number: int) -> None:
    """Record a response for circuit breaker tracking."""
    import time

    key = f"{repo}#{item_type}#{number}"
    with _circuit_breaker_lock:
        _circuit_breaker_state.setdefault(key, []).append(time.time())


def check_state(state: str, entity: str = "pr") -> str | GuardResult:
    """Returns skip reason (str) if closed/merged, GuardResult on pass."""
    if state in ("closed", "merged"):
        return f"{entity}_{state}"
    return GuardResult("state_check", "pass", f"{entity} {state}")


def check_blocked_label(labels: list[str]) -> str | GuardResult:
    """Returns skip reason (str) if blocked, GuardResult on pass."""
    if LABELS["blocked"] in labels:
        return "blocked_label"
    return GuardResult("blocked_label", "pass", "no blocked label")


# ---------------------------------------------------------------------------
# HMAC Verification
# ---------------------------------------------------------------------------

def verify_hmac(secret: bytes, payload: bytes, signature: str) -> bool:
    """Verify HMAC-SHA256 signature (constant-time comparison)."""
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def load_secret(path: Path) -> bytes:
    """Load webhook secret from file."""
    return path.read_text().strip().encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler for incoming GitHub webhook events."""

    # Set by the server factory
    queue: WorkQueue
    dispatcher: Dispatcher
    event_logger: Any  # EventLogger
    secret: bytes
    config: Config

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            data = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            payload_body = self.rfile.read(content_length)

            # 1. Verify HMAC signature
            signature = self.headers.get("X-Agent-Signature", "")
            if not verify_hmac(self.secret, payload_body, signature):
                log.warning("HMAC verification failed from %s", self.client_address)
                self._respond(403, {"error": "invalid signature"})
                return

            # 2. Parse payload
            try:
                payload = json.loads(payload_body)
            except json.JSONDecodeError:
                self._respond(400, {"error": "invalid JSON"})
                return

            event_type = payload.get("type", "unknown")
            repo = payload.get("repo", "unknown")
            number = payload.get("number", 0)

            # 3. Log receipt
            self.event_logger.log(
                "received",
                repo=repo,
                event_type=event_type,
                number=number,
            )
            prom.ISSUES_TOTAL.labels(repo=repo, action="received", reason="").inc()

            # 4. Run guards and route
            if event_type == "pr_comment":
                guard_result = self._check_pr_comment_guards(payload)
                if isinstance(guard_result, str):
                    self.event_logger.log(
                        "skipped",
                        repo=repo,
                        event_type=event_type,
                        number=number,
                        skip_reason=guard_result,
                    )
                    prom.ISSUES_TOTAL.labels(repo=repo, action="skipped", reason=guard_result).inc()
                    self._respond(200, {"skipped": guard_result})
                    return

                pr_number = payload.get("pr_number")
                self._emit_guard_checked(
                    guard_result, repo, event_type, number,
                    pr_number=pr_number,
                    comment_author=payload.get("comment_author", "unknown"),
                )

                item = QueueItem(
                    type="pr_comment",
                    number=payload.get("pr_number", number),
                    queued_at=QueueItem.now_iso(),
                    priority=True,
                    comment_id=payload.get("comment_id"),
                    pr_number=pr_number,
                    comment_body=payload.get("comment_body", "")[:10000],
                    comment_author=payload.get("comment_author", "unknown"),
                )

            elif event_type == "issue_comment":
                guard_result = self._check_issue_comment_guards(payload)
                if isinstance(guard_result, str):
                    self.event_logger.log(
                        "skipped",
                        repo=repo,
                        event_type=event_type,
                        number=number,
                        skip_reason=guard_result,
                    )
                    prom.ISSUES_TOTAL.labels(repo=repo, action="skipped", reason=guard_result).inc()
                    self._respond(200, {"skipped": guard_result})
                    return

                pr_number = payload.get("pr_number")
                self._emit_guard_checked(
                    guard_result, repo, event_type, number,
                    pr_number=pr_number,
                    comment_author=payload.get("comment_author", "unknown"),
                )

                item = QueueItem(
                    type="issue_comment",
                    number=number,
                    queued_at=QueueItem.now_iso(),
                    priority=True,
                    comment_id=payload.get("comment_id"),
                    pr_number=pr_number,
                    comment_body=payload.get("comment_body", "")[:10000],
                    comment_author=payload.get("comment_author", "unknown"),
                )

            elif event_type == "issue":
                labels = payload.get("labels", [])
                check = check_blocked_label(labels)
                if isinstance(check, str):
                    self.event_logger.log(
                        "skipped",
                        repo=repo,
                        event_type=event_type,
                        number=number,
                        skip_reason=check,
                    )
                    prom.ISSUES_TOTAL.labels(repo=repo, action="skipped", reason=check).inc()
                    self._respond(200, {"skipped": check})
                    return

                self._emit_guard_checked(
                    [check], repo, event_type, number,
                    pr_number=None,
                    comment_author=None,
                )

                item = QueueItem(
                    type="issue",
                    number=number,
                    queued_at=QueueItem.now_iso(),
                    title=payload.get("title", ""),
                    body=payload.get("body", ""),
                )

            elif event_type == "issue_closed":
                removed = self.queue.cancel(repo, number)
                if removed:
                    log.info("Canceled issue #%d for %s", number, repo)
                self._respond(202, {"canceled": removed})
                return

            else:
                self._respond(400, {"error": f"unknown event type: {event_type}"})
                return

            # 5. Enqueue
            enqueued = self.queue.enqueue(repo, item)
            if not enqueued:
                self.event_logger.log(
                    "skipped",
                    repo=repo,
                    event_type=event_type,
                    number=number,
                    skip_reason="duplicate",
                )
                prom.ISSUES_TOTAL.labels(repo=repo, action="skipped", reason="duplicate").inc()
                self._respond(200, {"skipped": "duplicate"})
                return

            depth = self.queue.get_depth(repo)
            self.event_logger.log(
                "queue_added",
                repo=repo,
                number=item.number,
                pr_number=item.pr_number,
                queue_depth=depth,
            )
            prom.QUEUE_DEPTH.labels(repo=repo).set(depth)
            prom.ISSUES_TOTAL.labels(repo=repo, action="queue_added", reason="").inc()

            # 6. Notify dispatcher
            self.dispatcher.ensure_repo_loop(repo)
            self.dispatcher.notify(repo)

            self._respond(202, {"queued": True, "depth": depth})

        except Exception:
            log.exception("Error handling webhook")
            self._respond(500, {"error": "internal error"})

    def _check_pr_comment_guards(
        self, payload: dict
    ) -> str | list[GuardResult]:
        """Run all guards for PR comment events.

        Returns skip reason (str) on first failure, or list of GuardResult on pass.
        """
        results: list[GuardResult] = []

        comment_body = payload.get("comment_body", "")
        check = check_self_reply(comment_body)
        if isinstance(check, str):
            return check
        results.append(check)

        pr_state = payload.get("pr_state", "open")
        check = check_state(pr_state, "pr")
        if isinstance(check, str):
            return check
        results.append(check)

        repo = payload.get("repo", "")
        pr_number = payload.get("pr_number", 0)
        check = check_circuit_breaker(
            repo,
            "pr_comment",
            pr_number,
            max_responses=self.config.circuit_breaker_max,
            window=self.config.circuit_breaker_window,
        )
        if isinstance(check, str):
            return check
        results.append(check)

        labels = payload.get("labels", [])
        check = check_blocked_label(labels)
        if isinstance(check, str):
            return check
        results.append(check)

        return results

    def _check_issue_comment_guards(
        self, payload: dict
    ) -> str | list[GuardResult]:
        """Run all guards for issue comment events.

        Returns skip reason (str) on first failure, or list of GuardResult on pass.
        """
        results: list[GuardResult] = []

        comment_body = payload.get("comment_body", "")
        check = check_self_reply(comment_body)
        if isinstance(check, str):
            return check
        results.append(check)

        issue_state = payload.get("issue_state", "open")
        check = check_state(issue_state, "issue")
        if isinstance(check, str):
            return check
        results.append(check)

        repo = payload.get("repo", "")
        number = payload.get("number", 0)
        check = check_circuit_breaker(
            repo,
            "issue_comment",
            number,
            max_responses=self.config.circuit_breaker_max,
            window=self.config.circuit_breaker_window,
        )
        if isinstance(check, str):
            return check
        results.append(check)

        labels = payload.get("labels", [])
        check = check_blocked_label(labels)
        if isinstance(check, str):
            return check
        results.append(check)

        return results

    def _emit_guard_checked(
        self,
        guard_results: list[GuardResult],
        repo: str,
        event_type: str,
        number: int,
        *,
        pr_number: int | None,
        comment_author: str | None,
    ) -> None:
        """Emit a guard_checked event with flattened audit fields."""
        guard_fields: dict[str, Any] = {}
        for gr in guard_results:
            guard_fields[f"guard_{gr.name}_result"] = gr.result
            guard_fields[f"guard_{gr.name}_detail"] = gr.detail

        self.event_logger.log(
            "guard_checked",
            repo=repo,
            event_type=event_type,
            number=number,
            pr_number=pr_number,
            comment_author=comment_author,
            **guard_fields,
        )

    def _respond(self, status: int, body: dict) -> None:
        """Send JSON response."""
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default HTTP logging — we use our own logger."""
        log.debug(format, *args)


def create_server(
    config: Config,
    queue: WorkQueue,
    dispatcher: Dispatcher,
    event_logger: Any,
) -> ThreadingHTTPServer:
    """Create and configure the webhook server."""
    secret = load_secret(config.secret_file)

    # Inject dependencies into handler class
    WebhookHandler.queue = queue
    WebhookHandler.dispatcher = dispatcher
    WebhookHandler.event_logger = event_logger
    WebhookHandler.secret = secret
    WebhookHandler.config = config

    server = ThreadingHTTPServer(
        (config.bind_address, config.port),
        WebhookHandler,
    )
    return server
