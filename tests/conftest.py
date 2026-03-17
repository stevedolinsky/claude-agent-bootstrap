"""Shared test fixtures for the receiver test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from receiver.dispatcher import EventLogger
from receiver.queue import WorkQueue
from receiver.server import Config


@pytest.fixture
def tmp_queue(tmp_path: Path) -> WorkQueue:
    """WorkQueue backed by a temp directory."""
    return WorkQueue(queue_dir=tmp_path / "queues")


@pytest.fixture
def tmp_events(tmp_path: Path) -> EventLogger:
    """EventLogger writing to a temp file."""
    return EventLogger(jsonl_path=tmp_path / "events.jsonl")


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    """Config using temp directories for all paths."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("test-secret-12345")
    secret_file.chmod(0o600)

    return Config(
        port=0,  # OS-assigned port
        queue_dir=tmp_path / "queues",
        events_file=tmp_path / "events.jsonl",
        secret_file=secret_file,
        budget_file=tmp_path / "budget.json",
        plans_dir=tmp_path / "plans",
        workers_dir=tmp_path / "workers",
        heartbeat_interval=1,
        worker_timeout_simple=10,
        worker_timeout_step=5,
        daily_budget_usd=50.0,
        per_worker_budget_usd=5.0,
    )
