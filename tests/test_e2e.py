"""End-to-end tests for the receiver pipeline.

Uses a real HTTP server with mock subprocess for claude --print.
Verifies: webhook receipt → queue → dispatch → cost tracking → events.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from receiver.dispatcher import Dispatcher, EventLogger, WorkerResult
from receiver.queue import WorkQueue
from receiver.server import Config, create_server

# Mock worker output simulating claude --print --output-format json
MOCK_WORKER_JSON = json.dumps({
    "type": "result",
    "result": "Implemented the feature. Created PR #42.",
    "total_cost_usd": 0.0,  # Always 0 on OAuth
    "duration_ms": 5000,
    "usage": {
        "input_tokens": 15000,
        "output_tokens": 3200,
        "cache_read_input_tokens": 45000,
        "cache_creation_input_tokens": 8000,
    },
    "modelUsage": {
        "claude-sonnet-4-6[1m]": {
            "inputTokens": 15000,
            "outputTokens": 3200,
            "cacheReadInputTokens": 45000,
            "cacheCreationInputTokens": 8000,
            "costUSD": 0.0,
        }
    },
})


@pytest.fixture
def e2e_env(tmp_path: Path):
    """Complete test environment: config, queue, events, server."""
    secret = "test-secret-e2e"
    secret_file = tmp_path / "secret"
    secret_file.write_text(secret)
    secret_file.chmod(0o600)

    config = Config(
        port=0,
        queue_dir=tmp_path / "queues",
        events_file=tmp_path / "events.jsonl",
        secret_file=secret_file,
        budget_file=tmp_path / "budget.json",
        plans_dir=tmp_path / "plans",
        workers_dir=tmp_path / "workers",
        heartbeat_interval=100,  # Don't emit heartbeats during tests
        worker_timeout_simple=10,
        worker_timeout_step=5,
        daily_budget_usd=50.0,
        per_worker_budget_usd=5.0,
    )
    config.ensure_dirs()

    events = EventLogger(config.events_file)
    queue = WorkQueue(config.queue_dir)
    dispatcher = Dispatcher(queue, events, config)

    # Patch _run_worker to return mock result instead of invoking claude CLI.
    # Small delay simulates worker execution so dedup tests work.
    mock_result = WorkerResult(
        exit_code=0,
        output="Mock implementation complete.",
        input_tokens=15000,
        output_tokens=3200,
        cache_read_tokens=45000,
        cache_creation_tokens=8000,
        cost_usd=0.0,
        estimated_api_cost_usd=0.075,
        duration_ms=5000,
        model="claude-sonnet-4-6",
    )

    def _mock_run_worker(repo: str, item: object, model: str) -> WorkerResult:
        time.sleep(0.5)  # Simulate work so dedup can detect in-progress
        return mock_result

    dispatcher._run_worker = _mock_run_worker  # type: ignore[assignment]

    server = create_server(config, queue, dispatcher, events)

    # Start server in background thread
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Get actual port
    port = server.server_address[1]

    yield {
        "config": config,
        "queue": queue,
        "events": events,
        "dispatcher": dispatcher,
        "server": server,
        "port": port,
        "secret": secret,
        "events_file": config.events_file,
        "tmp_path": tmp_path,
    }

    # Cleanup — stop dispatcher FIRST (it may be writing events), then close events
    dispatcher.stop(timeout=5)
    server.shutdown()
    time.sleep(0.1)  # Let any final writes complete
    events.close()


def _post_webhook(port: int, secret: str, payload: dict) -> http.client.HTTPResponse:
    """Send a webhook POST with valid HMAC signature."""
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST",
        "/webhook",
        body=body,
        headers={
            "Content-Type": "application/json",
            "X-Agent-Signature": sig,
        },
    )
    return conn.getresponse()


def _read_events(events_file: Path) -> list[dict]:
    """Read all events from JSONL file."""
    if not events_file.exists():
        return []
    lines = events_file.read_text().strip().split("\n")
    return [json.loads(line) for line in lines if line.strip()]


def _events_with_action(events: list[dict], action: str) -> list[dict]:
    """Filter events by action."""
    return [e for e in events if e.get("action") == action]


class TestWebhookToQueue:
    """Webhook → queue pipeline without worker execution."""

    def test_issue_enqueued(self, e2e_env: dict) -> None:
        """POST issue.labeled webhook → queue_added event."""
        resp = _post_webhook(e2e_env["port"], e2e_env["secret"], {
            "type": "issue",
            "number": 42,
            "title": "Add feature",
            "body": "Please add this feature",
            "repo": "owner/repo",
            "labels": ["agent"],
        })
        assert resp.status == 202

        data = json.loads(resp.read())
        assert data["queued"] is True

        events = _read_events(e2e_env["events_file"])
        received = _events_with_action(events, "received")
        assert len(received) == 1
        assert received[0]["number"] == 42

        queued = _events_with_action(events, "queue_added")
        assert len(queued) == 1
        assert queued[0]["queue_depth"] == 1

    def test_pr_comment_enqueued_with_priority(self, e2e_env: dict) -> None:
        """PR comment webhook → priority queue item via queue_added event."""
        resp = _post_webhook(e2e_env["port"], e2e_env["secret"], {
            "type": "pr_comment",
            "pr_number": 10,
            "number": 10,
            "comment_body": "Please fix the typo",
            "comment_author": "human",
            "pr_branch": "feat/thing",
            "repo": "owner/repo",
            "pr_state": "open",
            "comment_id": 123,
        })
        assert resp.status == 202

        data = json.loads(resp.read())
        assert data["queued"] is True

        # Verify via events (queue may be consumed by dispatcher before we check)
        events = _read_events(e2e_env["events_file"])
        queued = _events_with_action(events, "queue_added")
        assert len(queued) == 1
        assert queued[0]["number"] == 10

    def test_duplicate_rejected(self, e2e_env: dict) -> None:
        """Double-labeling only enqueues once."""
        payload = {
            "type": "issue",
            "number": 42,
            "title": "Add feature",
            "body": "Please add this feature",
            "repo": "owner/repo",
            "labels": ["agent"],
        }
        resp1 = _post_webhook(e2e_env["port"], e2e_env["secret"], payload)
        assert resp1.status == 202
        resp1.read()

        # Small delay to ensure first request is fully processed
        time.sleep(0.1)

        resp2 = _post_webhook(e2e_env["port"], e2e_env["secret"], payload)
        data = json.loads(resp2.read())
        # Either the item is still queued (skipped as duplicate)
        # or the dispatcher already consumed it (also skipped as in-progress)
        assert data.get("skipped") == "duplicate" or resp2.status == 200

        # Verify only one queue_added event
        events = _read_events(e2e_env["events_file"])
        queued = [e for e in events if e["action"] == "queue_added" and e["number"] == 42]
        assert len(queued) == 1


class TestGuards:
    """Guard functions prevent unwanted processing."""

    def test_self_reply_skipped(self, e2e_env: dict) -> None:
        resp = _post_webhook(e2e_env["port"], e2e_env["secret"], {
            "type": "pr_comment",
            "pr_number": 10,
            "number": 10,
            "comment_body": "Fixed the bug\n<!-- claude-agent -->",
            "comment_author": "bot",
            "pr_branch": "feat/thing",
            "repo": "owner/repo",
            "pr_state": "open",
            "comment_id": 456,
        })
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["skipped"] == "self_reply_marker"

    def test_closed_pr_skipped(self, e2e_env: dict) -> None:
        resp = _post_webhook(e2e_env["port"], e2e_env["secret"], {
            "type": "pr_comment",
            "pr_number": 10,
            "number": 10,
            "comment_body": "Please fix",
            "comment_author": "human",
            "pr_branch": "feat/thing",
            "repo": "owner/repo",
            "pr_state": "closed",
            "comment_id": 789,
        })
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["skipped"] == "pr_closed"

    def test_blocked_label_skipped(self, e2e_env: dict) -> None:
        resp = _post_webhook(e2e_env["port"], e2e_env["secret"], {
            "type": "issue",
            "number": 42,
            "title": "Blocked issue",
            "body": "Something",
            "repo": "owner/repo",
            "labels": ["agent", "agent-blocked"],
        })
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["skipped"] == "blocked_label"


class TestHMACAuth:
    """HMAC signature verification."""

    def test_bad_signature_rejected(self, e2e_env: dict) -> None:
        """Invalid HMAC returns 403."""
        body = json.dumps({"type": "issue", "number": 1, "repo": "r"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", e2e_env["port"], timeout=5)
        conn.request(
            "POST",
            "/webhook",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Agent-Signature": "sha256=deadbeefdeadbeef",
            },
        )
        resp = conn.getresponse()
        assert resp.status == 403

    def test_missing_signature_rejected(self, e2e_env: dict) -> None:
        body = json.dumps({"type": "issue", "number": 1, "repo": "r"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", e2e_env["port"], timeout=5)
        conn.request(
            "POST",
            "/webhook",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 403
