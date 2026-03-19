"""Unit tests for receiver.server — guards, HMAC, and config."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest

from receiver.server import (
    Config,
    LABELS,
    check_blocked_label,
    check_circuit_breaker,
    check_state,
    check_self_reply,
    record_circuit_breaker,
    verify_hmac,
    _circuit_breaker_state,
)


class TestSelfReplyGuard:
    """Self-reply detection via HTML marker and visible signature."""

    def test_detects_html_marker(self) -> None:
        body = "Some response text\n<!-- claude-agent -->"
        assert check_self_reply(body) == "self_reply_marker"

    def test_detects_visible_signature_sonnet(self) -> None:
        body = "Fixed the bug.\n\n· claude-sonnet-4-6"
        assert check_self_reply(body) == "self_reply_signature"

    def test_detects_visible_signature_opus(self) -> None:
        body = "Implemented feature · claude-opus-4-6"
        assert check_self_reply(body) == "self_reply_signature"

    def test_passes_human_comment(self) -> None:
        body = "Can you fix the error handling?"
        assert check_self_reply(body) is None

    def test_passes_empty_body(self) -> None:
        assert check_self_reply("") is None
        assert check_self_reply(None) is None  # type: ignore[arg-type]


class TestCircuitBreaker:
    """Circuit breaker limits responses per entity (type-scoped)."""

    def setup_method(self) -> None:
        _circuit_breaker_state.clear()

    def test_allows_first_response(self) -> None:
        assert check_circuit_breaker("r", "pr_comment", 1, max_responses=3, window=600) is None

    def test_trips_after_max(self) -> None:
        for _ in range(3):
            record_circuit_breaker("r", "pr_comment", 1)
        result = check_circuit_breaker("r", "pr_comment", 1, max_responses=3, window=600)
        assert result is not None
        assert "circuit_breaker" in result

    def test_different_prs_independent(self) -> None:
        for _ in range(3):
            record_circuit_breaker("r", "pr_comment", 1)
        # PR #2 should still be allowed
        assert check_circuit_breaker("r", "pr_comment", 2, max_responses=3, window=600) is None

    def test_different_types_independent(self) -> None:
        """Issue #1 and PR #1 have separate circuit breakers."""
        for _ in range(3):
            record_circuit_breaker("r", "pr_comment", 1)
        # Issue comment on #1 should still be allowed
        assert check_circuit_breaker("r", "issue_comment", 1, max_responses=3, window=600) is None


class TestStateGuard:
    """State guard for PRs and issues."""

    def test_rejects_closed_pr(self) -> None:
        assert check_state("closed", "pr") == "pr_closed"

    def test_rejects_merged_pr(self) -> None:
        assert check_state("merged", "pr") == "pr_merged"

    def test_allows_open_pr(self) -> None:
        assert check_state("open", "pr") is None

    def test_rejects_closed_issue(self) -> None:
        assert check_state("closed", "issue") == "issue_closed"

    def test_allows_open_issue(self) -> None:
        assert check_state("open", "issue") is None

    def test_defaults_to_pr(self) -> None:
        assert check_state("closed") == "pr_closed"


class TestBlockedLabelGuard:
    def test_rejects_blocked(self) -> None:
        assert check_blocked_label([LABELS["blocked"]]) == "blocked_label"

    def test_allows_no_blocked(self) -> None:
        assert check_blocked_label([LABELS["ready"], LABELS["wip"]]) is None

    def test_allows_empty(self) -> None:
        assert check_blocked_label([]) is None


class TestHMAC:
    """HMAC-SHA256 verification."""

    def test_valid_signature(self) -> None:
        secret = b"test-secret"
        payload = b'{"number": 42}'
        sig = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
        assert verify_hmac(secret, payload, sig) is True

    def test_invalid_signature(self) -> None:
        secret = b"test-secret"
        payload = b'{"number": 42}'
        assert verify_hmac(secret, payload, "sha256=deadbeef") is False

    def test_empty_signature(self) -> None:
        assert verify_hmac(b"secret", b"payload", "") is False

    def test_wrong_secret(self) -> None:
        payload = b'{"number": 42}'
        sig = "sha256=" + hmac.new(b"correct", payload, hashlib.sha256).hexdigest()
        assert verify_hmac(b"wrong", payload, sig) is False


class TestConfig:
    """Config dataclass."""

    def test_defaults(self) -> None:
        config = Config()
        assert config.port == 9876
        assert config.daily_budget_usd == 50.0
        assert config.per_worker_budget_usd == 5.0

    def test_path_expansion(self) -> None:
        config = Config()
        # Paths should be expanded (no ~)
        assert "~" not in str(config.queue_dir)

    def test_frozen(self) -> None:
        config = Config()
        with pytest.raises(AttributeError):
            config.port = 1234  # type: ignore[misc]

    def test_from_file(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "config.toml"
        toml_path.write_text('port = 8080\ndaily_budget_usd = 25.0\n')
        config = Config.from_file(toml_path)
        assert config.port == 8080
        assert config.daily_budget_usd == 25.0

    def test_ensure_dirs(self, test_config: Config) -> None:
        test_config.ensure_dirs()
        assert test_config.queue_dir.exists()
        assert test_config.plans_dir.exists()
        assert test_config.workers_dir.exists()


class TestPromptInjectionSafety:
    """Ensure shell metacharacters in issue content don't cause problems."""

    def test_metacharacters_in_title(self) -> None:
        """Guard functions should handle adversarial content safely."""
        dangerous_titles = [
            '$(rm -rf /)',
            '`whoami`',
            'test; rm -rf /',
            'test | cat /etc/passwd',
            'test\x00null',
            '"; DROP TABLE issues; --',
        ]
        for title in dangerous_titles:
            # Guards should not crash on adversarial input
            assert check_self_reply(title) is None
            assert check_blocked_label([title]) is None
