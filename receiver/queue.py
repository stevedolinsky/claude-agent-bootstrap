"""Persistent FIFO work queue with per-repo isolation and priority support."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .exceptions import QueueCorruptionError

log = logging.getLogger(__name__)


@dataclass(slots=True)
class QueueItem:
    """A single work item in the queue."""

    type: Literal["issue", "pr_comment", "issue_comment", "maintenance"]
    number: int
    queued_at: str  # ISO 8601
    priority: bool = False
    comment_id: int | None = None
    attempts: int = 0
    title: str = ""
    body: str = ""
    pr_number: int | None = None  # PR number when known (pr_comment, issue_comment on PR)
    comment_body: str = ""  # Comment text for pr_comment/issue_comment events
    comment_author: str = ""  # GitHub username who posted the comment

    @property
    def dedup_key(self) -> tuple:
        """Dedup key: (type, number, comment_id) for comments, (type, number) otherwise."""
        if self.comment_id is not None:
            return (self.type, self.number, self.comment_id)
        return (self.type, self.number)

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class WorkQueue:
    """Per-repo persistent FIFO queue.

    Queue is kept in memory for fast access and persisted to disk on every
    mutation via atomic write (tempfile + os.replace).

    Thread safety: one Lock per repo. The Event is used to wake dispatcher
    threads when new work arrives.
    """

    def __init__(self, queue_dir: Path) -> None:
        self._queue_dir = queue_dir
        self._queue_dir.mkdir(parents=True, exist_ok=True)

        # repo -> list of pending QueueItems
        self._queues: dict[str, list[QueueItem]] = {}
        # repo -> set of (type, number) currently in-progress
        self._in_progress: dict[str, set[tuple[str, int]]] = {}
        # repo -> Lock
        self._locks: dict[str, threading.Lock] = {}
        # repo -> Event (signaled when new work arrives)
        self._events: dict[str, threading.Event] = {}

        self._load_all()

    # --- Public API ---

    def enqueue(self, repo: str, item: QueueItem) -> bool:
        """Add item to repo's queue. Returns False if duplicate."""
        lock = self._ensure_repo(repo)
        with lock:
            key = item.dedup_key

            # Dedup: reject if already queued or in-progress
            for existing in self._queues[repo]:
                if existing.dedup_key == key:
                    log.debug("dedup: %s already queued for %s", key, repo)
                    return False
            if key in self._in_progress[repo]:
                log.debug("dedup: %s already in-progress for %s", key, repo)
                return False

            self._queues[repo].append(item)
            self._persist(repo)

        # Signal outside the lock so dispatcher can wake up
        self._events[repo].set()
        return True

    def take_next(self, repo: str) -> QueueItem | None:
        """Atomically pop next item and mark in-progress.

        Priority items first, then FIFO. Returns None if empty.
        """
        lock = self._ensure_repo(repo)
        with lock:
            items = self._queues[repo]
            if not items:
                return None

            # Find first priority item, or fall back to first item
            idx = next(
                (i for i, item in enumerate(items) if item.priority),
                0,
            )
            item = items.pop(idx)
            self._in_progress[repo].add(item.dedup_key)
            self._persist(repo)
            return item

    def complete(self, repo: str, dedup_key: tuple) -> None:
        """Remove item from in-progress tracking."""
        lock = self._ensure_repo(repo)
        with lock:
            self._in_progress[repo].discard(dedup_key)

    def cancel(self, repo: str, issue_number: int) -> bool:
        """Remove item by issue number. Returns True if found."""
        lock = self._ensure_repo(repo)
        with lock:
            before = len(self._queues[repo])
            self._queues[repo] = [
                item
                for item in self._queues[repo]
                if item.number != issue_number
            ]
            removed = len(self._queues[repo]) < before
            if removed:
                self._persist(repo)
            return removed

    def requeue_front(self, repo: str, item: QueueItem) -> None:
        """Add item to front of queue (for epic continuation)."""
        lock = self._ensure_repo(repo)
        with lock:
            self._in_progress[repo].discard(item.dedup_key)
            self._queues[repo].insert(0, item)
            self._persist(repo)
        self._events[repo].set()

    def get_depth(self, repo: str) -> int:
        """Current queue depth (pending items only)."""
        lock = self._ensure_repo(repo)
        with lock:
            return len(self._queues[repo])

    def get_items(self, repo: str) -> list[QueueItem]:
        """All pending items (snapshot for dashboard)."""
        lock = self._ensure_repo(repo)
        with lock:
            return list(self._queues[repo])

    def repos(self) -> list[str]:
        """All known repos."""
        return list(self._queues.keys())

    def wait_for_work(self, repo: str, timeout: float | None = None) -> bool:
        """Block until work is available or timeout. Returns True if signaled."""
        event = self._events.setdefault(repo, threading.Event())
        result = event.wait(timeout=timeout)
        event.clear()
        return result

    def wake(self, repo: str) -> None:
        """Wake any thread waiting on this repo's event."""
        event = self._events.get(repo)
        if event is not None:
            event.set()

    # --- Internals ---

    def _ensure_repo(self, repo: str) -> threading.Lock:
        """Ensure data structures exist for this repo."""
        if repo not in self._locks:
            self._locks[repo] = threading.Lock()
            self._queues.setdefault(repo, [])
            self._in_progress.setdefault(repo, set())
            self._events.setdefault(repo, threading.Event())
        return self._locks[repo]

    def _queue_file(self, repo: str) -> Path:
        """Queue file path for a repo. Slugifies owner/repo."""
        slug = repo.replace("/", "-")
        return self._queue_dir / f"{slug}.json"

    def _persist(self, repo: str) -> None:
        """Atomic write queue to disk."""
        path = self._queue_file(repo)
        data = [asdict(item) for item in self._queues[repo]]
        try:
            fd, tmp = tempfile.mkstemp(dir=self._queue_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, path)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError:
            log.exception("Failed to persist queue for %s", repo)

    def _load_all(self) -> None:
        """Load all queue files from disk on startup."""
        for path in self._queue_dir.glob("*.json"):
            repo_slug = path.stem
            # Convert slug back to owner/repo (best effort)
            parts = repo_slug.split("-", 1)
            repo = "/".join(parts) if len(parts) == 2 else repo_slug
            try:
                data = json.loads(path.read_text())
                items = [QueueItem(**item) for item in data]
                self._queues[repo] = items
                self._in_progress[repo] = set()
                self._locks[repo] = threading.Lock()
                self._events[repo] = threading.Event()
                if items:
                    self._events[repo].set()
                log.info("Loaded %d items for %s", len(items), repo)
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                corrupt_path = path.with_suffix(".corrupt")
                path.rename(corrupt_path)
                log.error(
                    "Corrupt queue file %s, renamed to %s: %s",
                    path,
                    corrupt_path,
                    exc,
                )
                raise QueueCorruptionError(str(path)) from exc
