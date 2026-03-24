# Brainstorm: Agent Re-fire Visibility in Dashboard

**Date:** 2026-03-24
**Status:** Draft

## What We're Building

Dashboard visibility into when the agent re-intakes its own comments and performs duplicate work. Two new capabilities:

1. **Per-PR Timeline Panel** — A Grafana panel that shows all agent events for a single PR/issue as a chronological timeline, so you can see the chain: dispatch → work → comment → re-dispatch → duplicate work.

2. **Guard Decision Logging** — Enrich the JSONL event stream so that every webhook that *passes* all guards logs the full audit trail (marker check, signature check, circuit breaker state), making it clear exactly why a self-reply wasn't caught.

## The Problem

Current flow that causes duplicate work:
1. User labels issue → agent creates PR
2. User comments on PR with feedback
3. Agent picks up feedback, works for a while, comments on PR with its response
4. That agent comment triggers a new GitHub webhook
5. By the time it arrives, the circuit breaker window (10 min) has expired
6. Self-reply guard (marker/signature check) doesn't catch it for unknown reasons
7. New worker dispatched → agent responds to its own comment → duplicate work

The dashboard currently shows individual events but lacks:
- Correlation across events for the same PR (no way to see "PR #42 was dispatched 3 times")
- Guard pass-through detail (you can see skips but not *why* something was allowed through)

## Why This Approach

**Enrich existing JSONL events** (not a separate log or Prometheus metrics) because:
- Uses the existing pipeline: JSONL → Promtail → Loki → Grafana
- No new infrastructure components
- Single source of truth for all agent events
- Grafana's LogQL is well-suited for filtering by field values (pr_number)

Rejected alternatives:
- **Separate guard audit log** — extra Promtail job, harder to correlate with main events
- **Prometheus labels** — cardinality explosion with PR numbers, loses event detail

## Key Decisions

1. **Enrich JSONL, don't split logs** — All events stay in `agent-events.jsonl`, guard audit is additional fields on existing events.

2. **Two-tier verbosity for guard logging:**
   - **Pass-throughs** (webhook accepted): Full audit — each guard check result, comment author, marker presence, signature match, circuit breaker state/window.
   - **Skips** (webhook rejected): Keep existing summary event (`skipped` action with `reason` field). No change needed.

3. **`pr_number` field on all events** — Every event that can be associated with a PR includes `pr_number` for Grafana filtering. Issue events include `issue_number`. Events after PR creation include both.

4. **PR Timeline is variable-driven** — Grafana panel uses a dashboard variable (dropdown or text input) to select a PR number, then shows all events filtered to that PR.

## Implementation Scope

### Receiver changes (claude-agent-bootstrap)

- Add `pr_number` field to event emission wherever available (dispatch, triage, spawned, done, blocked, cost_tracked events)
- Add `guard_checked` action type for pass-throughs with fields:
  ```json
  {
    "ts": "...",
    "action": "guard_checked",
    "repo": "owner/repo",
    "number": 42,
    "pr_number": 42,
    "result": "passed",
    "comment_author": "github-actions[bot]",
    "guards": {
      "self_reply_marker": {"result": "pass", "detail": "no marker found"},
      "self_reply_signature": {"result": "pass", "detail": "no signature match"},
      "circuit_breaker": {"result": "pass", "detail": "window expired, last_seen: 2026-03-24T14:20:00Z, count: 0"},
      "state_check": {"result": "pass", "detail": "PR open"},
      "blocked_label": {"result": "pass", "detail": "no blocked label"}
    }
  }
  ```

### Dashboard changes (claude-agent-dashboard)

- **PR Timeline Panel**: Logs panel filtered by `pr_number` variable, showing events ordered by timestamp. Columns: timestamp, action, model, guard result, duration.
- **Guard Audit Panel**: Table panel showing `guard_checked` events with expandable guard detail. Filterable by repo, time range, and result.

## Open Questions

None — all key decisions resolved during brainstorming.
