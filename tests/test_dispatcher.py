"""Unit tests for receiver.dispatcher — EventLogger, cost estimation, budget tracking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from receiver.dispatcher import (
    API_PRICING,
    DailyBudget,
    EventLogger,
    WorkerResult,
    estimate_api_cost,
    load_budget,
    parse_worker_output,
    save_budget,
    VALID_ACTIONS,
)


class TestEventLogger:
    """EventLogger writes valid JSONL."""

    def test_writes_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        logger = EventLogger(path)
        logger.log("received", repo="owner/repo", number=42)
        logger.close()

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["action"] == "received"
        assert event["repo"] == "owner/repo"
        assert event["number"] == 42
        assert "ts" in event

    def test_strips_none_values(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        logger = EventLogger(path)
        logger.log("skipped", repo="r", number=1, skip_reason="test", extra=None)
        logger.close()

        event = json.loads(path.read_text().strip())
        assert "extra" not in event
        assert event["skip_reason"] == "test"

    def test_default_repo(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        logger = EventLogger(path, repo_default="default/repo")
        logger.log("heartbeat")
        logger.close()

        event = json.loads(path.read_text().strip())
        assert event["repo"] == "default/repo"

    def test_multiple_events(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        logger = EventLogger(path)
        for i in range(10):
            logger.log("received", number=i)
        logger.close()

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 10


class TestWorkerOutputParsing:
    """Parse claude --print --output-format json output."""

    MOCK_OUTPUT = json.dumps({
        "type": "result",
        "result": "Implemented the feature.",
        "total_cost_usd": 0.0,
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
                "costUSD": 0.0,
            }
        },
    })

    def test_parse_json_output(self) -> None:
        result = parse_worker_output(self.MOCK_OUTPUT, exit_code=0)
        assert result.exit_code == 0
        assert result.output == "Implemented the feature."
        assert result.input_tokens == 15000
        assert result.output_tokens == 3200
        assert result.cache_read_tokens == 45000
        assert result.cache_creation_tokens == 8000
        assert result.cost_usd == 0.0
        assert result.duration_ms == 5000
        assert result.model == "claude-sonnet-4-6"
        assert result.estimated_api_cost_usd > 0

    def test_parse_plain_text(self) -> None:
        result = parse_worker_output("Just some text output", exit_code=0)
        assert result.output == "Just some text output"
        assert result.input_tokens == 0

    def test_parse_empty_output(self) -> None:
        result = parse_worker_output("", exit_code=1)
        assert result.exit_code == 1
        assert result.output == ""

    def test_model_normalization(self) -> None:
        """Model ID with context window suffix is normalized."""
        result = parse_worker_output(self.MOCK_OUTPUT, exit_code=0)
        assert result.model == "claude-sonnet-4-6"  # Not "claude-sonnet-4-6[1m]"


class TestCostEstimation:
    """API-equivalent cost from token counts."""

    def test_sonnet_cost(self) -> None:
        result = WorkerResult(
            exit_code=0,
            input_tokens=1_000_000,
            output_tokens=100_000,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            model="claude-sonnet-4-6",
        )
        cost = estimate_api_cost(result)
        # 1M input * $3/M + 100K output * $15/M = $3 + $1.5 = $4.5
        assert abs(cost - 4.5) < 0.01

    def test_opus_cost(self) -> None:
        result = WorkerResult(
            exit_code=0,
            input_tokens=1_000_000,
            output_tokens=100_000,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            model="claude-opus-4-6",
        )
        cost = estimate_api_cost(result)
        # 1M input * $15/M + 100K output * $75/M = $15 + $7.5 = $22.5
        assert abs(cost - 22.5) < 0.01

    def test_cache_tokens_included(self) -> None:
        result = WorkerResult(
            exit_code=0,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=1_000_000,
            cache_creation_tokens=1_000_000,
            model="claude-sonnet-4-6",
        )
        cost = estimate_api_cost(result)
        # 1M cache_read * $0.30/M + 1M cache_create * $3.75/M = $0.30 + $3.75 = $4.05
        assert abs(cost - 4.05) < 0.01

    def test_unknown_model_defaults_to_sonnet(self) -> None:
        result = WorkerResult(
            exit_code=0,
            input_tokens=1_000_000,
            output_tokens=0,
            model="claude-unknown-99",
        )
        cost = estimate_api_cost(result)
        assert abs(cost - 3.0) < 0.01  # Sonnet input rate


class TestDailyBudget:
    """Budget tracking and persistence."""

    def test_reset_on_new_day(self) -> None:
        budget = DailyBudget(date="2026-01-01", estimated_cost_usd=99.0, worker_count=50)
        budget.reset_if_new_day()
        # Date should now be today, cost should be 0
        assert budget.estimated_cost_usd == 0.0
        assert budget.worker_count == 0

    def test_add_worker_result(self) -> None:
        budget = DailyBudget()
        budget.reset_if_new_day()
        result = WorkerResult(
            exit_code=0,
            input_tokens=10000,
            output_tokens=2000,
            estimated_api_cost_usd=0.50,
        )
        budget.add(result)
        assert budget.total_input_tokens == 10000
        assert budget.total_output_tokens == 2000
        assert abs(budget.estimated_cost_usd - 0.50) < 0.001
        assert budget.worker_count == 1

    def test_save_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "budget.json"
        budget = DailyBudget()
        budget.reset_if_new_day()
        budget.estimated_cost_usd = 12.34
        budget.worker_count = 5
        save_budget(path, budget)

        loaded = load_budget(path)
        assert loaded.estimated_cost_usd == 12.34
        assert loaded.worker_count == 5

    def test_corrupt_budget_starts_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / "budget.json"
        path.write_text("{corrupt")
        budget = load_budget(path)
        assert budget.estimated_cost_usd == 0.0


class TestValidActions:
    """Event action validation."""

    def test_all_expected_actions_present(self) -> None:
        expected = {
            "received", "skipped", "spawned", "done", "error", "heartbeat",
            "queue_added", "dispatched", "plan_created", "step_started",
            "step_completed", "pr_created", "blocked",
            "cost_tracked", "budget_exhausted", "triage",
            "guard_checked", "receiver_started",
        }
        assert VALID_ACTIONS == expected
