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

def check_self_reply(comment_body: str) -> str | None:
    """Returns skip reason if self-reply detected, None if safe."""
    if not comment_body:
        return None
    # Layer 1: HTML marker
    if "<!-- claude-agent -->" in comment_body:
        return "self_reply_marker"
    # Layer 2: Visible signature
    for sig in ("· claude-sonnet-", "· claude-opus-", "· claude-haiku-"):
        if sig in comment_body:
            return "self_reply_signature"
    return None


# Module-level state for circuit breaker (in-memory, not persisted)
_circuit_breaker_state: dict[str, list[float]] = {}
_circuit_breaker_lock = threading.Lock()


def check_circuit_breaker(
    repo: str, item_type: str, number: int, max_responses: int = 3, window: int = 600
) -> str | None:
    """Returns skip reason if item has exceeded response limit."""
    import time

    key = f"{repo}#{item_type}#{number}"
    now = time.monotonic()

    with _circuit_breaker_lock:
        timestamps = _circuit_breaker_state.get(key, [])
        # Prune expired entries
        timestamps = [t for t in timestamps if now - t < window]
        _circuit_breaker_state[key] = timestamps

        if len(timestamps) >= max_responses:
            return f"circuit_breaker_{len(timestamps)}_in_{window}s"
        return None


def record_circuit_breaker(repo: str, item_type: str, number: int) -> None:
    """Record a response for circuit breaker tracking."""
    import time

    key = f"{repo}#{item_type}#{number}"
    with _circuit_breaker_lock:
        _circuit_breaker_state.setdefault(key, []).append(time.monotonic())


def check_state(state: str, entity: str = "pr") -> str | None:
    """Returns skip reason if entity is closed/merged."""
    if state in ("closed", "merged"):
        return f"{entity}_{state}"
    return None


def check_blocked_label(labels: list[str]) -> str | None:
    """Returns skip reason if agent-blocked label present."""
    if LABELS["blocked"] in labels:
        return "blocked_label"
    return None


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
                skip = self._check_pr_comment_guards(payload)
                if skip:
                    self.event_logger.log(
                        "skipped",
                        repo=repo,
                        event_type=event_type,
                        number=number,
                        skip_reason=skip,
                    )
                    prom.ISSUES_TOTAL.labels(repo=repo, action="skipped", reason=skip).inc()
                    self._respond(200, {"skipped": skip})
                    return

                item = QueueItem(
                    type="pr_comment",
                    number=payload.get("pr_number", number),
                    queued_at=QueueItem.now_iso(),
                    priority=True,
                    comment_id=payload.get("comment_id"),
                )

            elif event_type == "issue_comment":
                skip = self._check_issue_comment_guards(payload)
                if skip:
                    self.event_logger.log(
                        "skipped",
                        repo=repo,
                        event_type=event_type,
                        number=number,
                        skip_reason=skip,
                    )
                    prom.ISSUES_TOTAL.labels(repo=repo, action="skipped", reason=skip).inc()
                    self._respond(200, {"skipped": skip})
                    return

                item = QueueItem(
                    type="issue_comment",
                    number=number,
                    queued_at=QueueItem.now_iso(),
                    priority=True,
                    comment_id=payload.get("comment_id"),
                )

            elif event_type == "issue":
                labels = payload.get("labels", [])
                skip = check_blocked_label(labels)
                if skip:
                    self.event_logger.log(
                        "skipped",
                        repo=repo,
                        event_type=event_type,
                        number=number,
                        skip_reason=skip,
                    )
                    prom.ISSUES_TOTAL.labels(repo=repo, action="skipped", reason=skip).inc()
                    self._respond(200, {"skipped": skip})
                    return

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

    def _check_pr_comment_guards(self, payload: dict) -> str | None:
        """Run all guards for PR comment events."""
        comment_body = payload.get("comment_body", "")
        skip = check_self_reply(comment_body)
        if skip:
            return skip

        pr_state = payload.get("pr_state", "open")
        skip = check_state(pr_state, "pr")
        if skip:
            return skip

        repo = payload.get("repo", "")
        pr_number = payload.get("pr_number", 0)
        skip = check_circuit_breaker(
            repo,
            "pr_comment",
            pr_number,
            max_responses=self.config.circuit_breaker_max,
            window=self.config.circuit_breaker_window,
        )
        if skip:
            return skip

        labels = payload.get("labels", [])
        skip = check_blocked_label(labels)
        if skip:
            return skip

        return None

    def _check_issue_comment_guards(self, payload: dict) -> str | None:
        """Run all guards for issue comment events."""
        comment_body = payload.get("comment_body", "")
        skip = check_self_reply(comment_body)
        if skip:
            return skip

        issue_state = payload.get("issue_state", "open")
        skip = check_state(issue_state, "issue")
        if skip:
            return skip

        repo = payload.get("repo", "")
        number = payload.get("number", 0)
        skip = check_circuit_breaker(
            repo,
            "issue_comment",
            number,
            max_responses=self.config.circuit_breaker_max,
            window=self.config.circuit_breaker_window,
        )
        if skip:
            return skip

        labels = payload.get("labels", [])
        skip = check_blocked_label(labels)
        if skip:
            return skip

        return None

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
