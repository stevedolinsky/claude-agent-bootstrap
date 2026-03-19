"""Unit tests for receiver.queue."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from receiver.exceptions import QueueCorruptionError
from receiver.queue import QueueItem, WorkQueue


class TestEnqueueDequeue:
    """Basic FIFO semantics."""

    def test_fifo_order(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        item1 = QueueItem(type="issue", number=1, queued_at="2026-01-01T00:00:00Z")
        item2 = QueueItem(type="issue", number=2, queued_at="2026-01-01T00:01:00Z")

        tmp_queue.enqueue(repo, item1)
        tmp_queue.enqueue(repo, item2)

        result1 = tmp_queue.take_next(repo)
        result2 = tmp_queue.take_next(repo)

        assert result1 is not None
        assert result1.number == 1
        assert result2 is not None
        assert result2.number == 2

    def test_empty_queue_returns_none(self, tmp_queue: WorkQueue) -> None:
        result = tmp_queue.take_next("owner/repo")
        assert result is None


class TestDedup:
    """Deduplication on (type, number)."""

    def test_rejects_duplicate_issue(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        item = QueueItem(type="issue", number=42, queued_at="2026-01-01T00:00:00Z")

        assert tmp_queue.enqueue(repo, item) is True
        assert tmp_queue.enqueue(repo, item) is False

    def test_rejects_in_progress_duplicate(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        item = QueueItem(type="issue", number=42, queued_at="2026-01-01T00:00:00Z")

        tmp_queue.enqueue(repo, item)
        tmp_queue.take_next(repo)  # Now in-progress

        # Try to enqueue same issue again
        item2 = QueueItem(type="issue", number=42, queued_at="2026-01-01T00:01:00Z")
        assert tmp_queue.enqueue(repo, item2) is False

    def test_allows_different_types_same_number(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        issue = QueueItem(type="issue", number=42, queued_at="2026-01-01T00:00:00Z")
        comment = QueueItem(
            type="pr_comment", number=42, queued_at="2026-01-01T00:00:00Z",
            priority=True, comment_id=123,
        )

        assert tmp_queue.enqueue(repo, issue) is True
        assert tmp_queue.enqueue(repo, comment) is True


class TestCommentDedup:
    """Comment-level dedup: different comment_ids on same number are separate items."""

    def test_different_comments_same_pr_both_enqueued(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        c1 = QueueItem(
            type="pr_comment", number=10, queued_at="2026-01-01T00:00:00Z",
            priority=True, comment_id=100,
        )
        c2 = QueueItem(
            type="pr_comment", number=10, queued_at="2026-01-01T00:01:00Z",
            priority=True, comment_id=200,
        )

        assert tmp_queue.enqueue(repo, c1) is True
        assert tmp_queue.enqueue(repo, c2) is True
        assert tmp_queue.get_depth(repo) == 2

    def test_same_comment_id_rejected(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        c1 = QueueItem(
            type="pr_comment", number=10, queued_at="2026-01-01T00:00:00Z",
            priority=True, comment_id=100,
        )
        c2 = QueueItem(
            type="pr_comment", number=10, queued_at="2026-01-01T00:01:00Z",
            priority=True, comment_id=100,
        )

        assert tmp_queue.enqueue(repo, c1) is True
        assert tmp_queue.enqueue(repo, c2) is False

    def test_issue_comment_dedup(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        c1 = QueueItem(
            type="issue_comment", number=42, queued_at="2026-01-01T00:00:00Z",
            priority=True, comment_id=300,
        )
        c2 = QueueItem(
            type="issue_comment", number=42, queued_at="2026-01-01T00:01:00Z",
            priority=True, comment_id=400,
        )

        assert tmp_queue.enqueue(repo, c1) is True
        assert tmp_queue.enqueue(repo, c2) is True

    def test_complete_with_comment_dedup_key(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        c1 = QueueItem(
            type="pr_comment", number=10, queued_at="2026-01-01T00:00:00Z",
            priority=True, comment_id=100,
        )
        tmp_queue.enqueue(repo, c1)
        taken = tmp_queue.take_next(repo)
        assert taken is not None

        # Complete using dedup_key
        tmp_queue.complete(repo, taken.dedup_key)

        # Should now be able to enqueue same comment again
        c2 = QueueItem(
            type="pr_comment", number=10, queued_at="2026-01-01T00:02:00Z",
            priority=True, comment_id=100,
        )
        assert tmp_queue.enqueue(repo, c2) is True


class TestPriority:
    """Priority items processed before regular items."""

    def test_priority_first(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        regular = QueueItem(type="issue", number=1, queued_at="2026-01-01T00:00:00Z")
        priority = QueueItem(
            type="pr_comment", number=2, queued_at="2026-01-01T00:01:00Z",
            priority=True, comment_id=5,
        )

        tmp_queue.enqueue(repo, regular)
        tmp_queue.enqueue(repo, priority)

        result = tmp_queue.take_next(repo)
        assert result is not None
        assert result.number == 2
        assert result.priority is True


class TestCancel:
    """Cancel removes queued items."""

    def test_cancel_removes_item(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        item = QueueItem(type="issue", number=42, queued_at="2026-01-01T00:00:00Z")
        tmp_queue.enqueue(repo, item)

        assert tmp_queue.cancel(repo, 42) is True
        assert tmp_queue.get_depth(repo) == 0

    def test_cancel_nonexistent_returns_false(self, tmp_queue: WorkQueue) -> None:
        assert tmp_queue.cancel("owner/repo", 999) is False


class TestPersistence:
    """Queue survives reload from disk."""

    def test_persist_and_reload(self, tmp_path: Path) -> None:
        queue_dir = tmp_path / "queues"
        repo = "owner/repo"
        item = QueueItem(type="issue", number=42, queued_at="2026-01-01T00:00:00Z")

        # Write
        q1 = WorkQueue(queue_dir=queue_dir)
        q1.enqueue(repo, item)

        # Reload
        q2 = WorkQueue(queue_dir=queue_dir)
        assert q2.get_depth(repo) == 1
        result = q2.take_next(repo)
        assert result is not None
        assert result.number == 42

    def test_corrupt_file_recovery(self, tmp_path: Path) -> None:
        queue_dir = tmp_path / "queues"
        queue_dir.mkdir(parents=True)

        # Write corrupt JSON
        (queue_dir / "owner-repo.json").write_text("{invalid json")

        with pytest.raises(QueueCorruptionError):
            WorkQueue(queue_dir=queue_dir)

        # Corrupt file should be renamed
        assert (queue_dir / "owner-repo.corrupt").exists()
        assert not (queue_dir / "owner-repo.json").exists()


class TestAttempts:
    """Attempts counter for retry tracking."""

    def test_attempts_preserved(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        item = QueueItem(
            type="issue", number=42, queued_at="2026-01-01T00:00:00Z", attempts=2,
        )
        tmp_queue.enqueue(repo, item)
        result = tmp_queue.take_next(repo)
        assert result is not None
        assert result.attempts == 2


class TestDepthAndItems:
    """Queue depth and item listing."""

    def test_depth(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        for i in range(5):
            tmp_queue.enqueue(
                repo,
                QueueItem(type="issue", number=i, queued_at="2026-01-01T00:00:00Z"),
            )
        assert tmp_queue.get_depth(repo) == 5

    def test_get_items_returns_snapshot(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        tmp_queue.enqueue(
            repo,
            QueueItem(type="issue", number=1, queued_at="2026-01-01T00:00:00Z"),
        )
        items = tmp_queue.get_items(repo)
        assert len(items) == 1
        assert items[0].number == 1


class TestRequeueFront:
    """Requeue to front for epic continuation."""

    def test_requeue_front(self, tmp_queue: WorkQueue) -> None:
        repo = "owner/repo"
        item1 = QueueItem(type="issue", number=1, queued_at="2026-01-01T00:00:00Z")
        item2 = QueueItem(type="issue", number=2, queued_at="2026-01-01T00:01:00Z")

        tmp_queue.enqueue(repo, item1)
        # Take item1
        taken = tmp_queue.take_next(repo)
        assert taken is not None
        assert taken.number == 1

        # Enqueue item2
        tmp_queue.enqueue(repo, item2)

        # Requeue item1 to front
        tmp_queue.requeue_front(repo, taken)

        # Should get item1 first (front), then item2
        result = tmp_queue.take_next(repo)
        assert result is not None
        assert result.number == 1
