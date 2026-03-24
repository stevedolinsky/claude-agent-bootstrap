---
title: "feat: Agent Re-fire Visibility in Dashboard"
type: feat
status: active
date: 2026-03-24
origin: docs/brainstorms/2026-03-24-agent-refire-visibility-brainstorm.md
---

# feat: Agent Re-fire Visibility in Dashboard

## Overview

Add two new dashboard capabilities to diagnose when the agent re-intakes its own comments and performs duplicate work: a **Per-PR Timeline Panel** showing all events for a single PR chronologically, and **Guard Decision Logging** that records the full audit trail when a webhook passes all guards.

## Problem Statement / Motivation

When the agent responds to PR feedback, it sometimes takes a while to complete work and then comments on the PR. That comment triggers a new GitHub webhook. By the time it arrives, the circuit breaker window (10 min) has expired, and for unknown reasons the self-reply marker/signature checks don't catch it. A new worker is dispatched, creating duplicate work.

The dashboard currently shows individual events but lacks:
- **Correlation**: No way to see "PR #42 was dispatched 3 times" — events are flat with no PR-level grouping
- **Guard transparency**: `skipped` events log the rejection reason, but webhooks that *pass* all guards leave zero audit trail

Without visibility, the root cause of self-reply bypass cannot be diagnosed. (see brainstorm: docs/brainstorms/2026-03-24-agent-refire-visibility-brainstorm.md)

## Proposed Solution

Enrich the existing JSONL event pipeline (not separate logs or Prometheus labels) with two additions:

1. **`guard_checked` event** — emitted when a webhook passes ALL guards, with flattened audit fields for each guard check
2. **`pr_number` field** — added to all events that can be associated with a PR, enabling Grafana filtering by PR

Then add two new Grafana panels to the Repo Detail dashboard in `claude-agent-dashboard`.

## Technical Considerations

### Architecture
- Changes span **two repos**: `claude-agent-bootstrap` (receiver) and `claude-agent-dashboard` (Grafana)
- Uses existing pipeline: JSONL → Promtail → Loki → Grafana — no new infrastructure
- Guard audit fields are **flattened** (not nested) for LogQL queryability

### Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| `pr_number` location | QueueItem field + JSONL events | Dispatcher needs it to emit in its events (dispatched, spawned, done) |
| Guard audit format | Flattened top-level fields | LogQL filters top-level JSON easily; nested objects require JSONPath extraction |
| Guard behavior on fail | Keep short-circuit | Only pass-throughs get full audit; skips keep existing `skip_reason` summary |
| Circuit breaker timestamps | Switch to wall-clock (`time.time()`) | Monotonic clock can't be reported as ISO timestamps in audit detail |
| `$number` variable | Text input, empty = guidance message | Avoid noisy "show all" default; user must know their PR number |
| Issue event guard_checked | Yes, emit for all event types | Even single-guard checks (blocked_label) should be auditable |
| `receiver_started` event | Add it | Distinguishes "circuit breaker expired" from "receiver restarted" |

### Backward Compatibility
- `number` field kept unchanged — `pr_number` is additive
- QueueItem gets `pr_number: int | None = None` — existing queue files deserialize safely
- Existing `skipped` events unchanged — no migration needed
- Old events without `pr_number` show as empty in Grafana (handled gracefully)

## Acceptance Criteria

- [x] `guard_checked` event emitted for every webhook that passes all guards
- [x] Each guard check (marker, signature, circuit breaker, state, blocked_label) logged with result and detail
- [x] `pr_number` field present on all PR-related events (received, guard_checked, queue_added, dispatched, spawned, done, cost_tracked, blocked)
- [x] `receiver_started` event emitted on receiver startup
- [ ] PR Timeline panel in Grafana shows all events for a selected PR number (dashboard repo)
- [ ] Guard Audit panel in Grafana shows `guard_checked` events with per-guard detail (dashboard repo)
- [x] Existing tests pass; new tests cover guard audit structure
- [x] Guard detail strings never include user-generated content (comment body)

## Implementation Plan

### Phase 1: Receiver — Guard Audit Logging

**Files:** `receiver/server.py`, `receiver/dispatcher.py`, `tests/test_server.py`, `tests/test_dispatcher.py`

#### 1a. Define GuardResult dataclass

`receiver/server.py` — add near the top:

```python
@dataclass
class GuardResult:
    name: str      # e.g., "self_reply_marker"
    result: str    # "pass" or "fail"
    detail: str    # human-readable, no user-generated content
```

#### 1b. Refactor guard methods to return GuardResult on pass

Each guard method currently returns `str | None`. Change to return `GuardResult` on pass, `str` (skip reason) on fail. The caller checks the type:

- `check_self_reply()` → returns `GuardResult("self_reply_marker", "pass", "no marker found")` on pass
- `check_circuit_breaker()` → returns `GuardResult("circuit_breaker", "pass", "window_expired, last_seen=2026-03-24T14:20:00Z, count=0")` on pass
- `check_state()` → returns `GuardResult("state_check", "pass", "PR open")` on pass
- `check_blocked_label()` → returns `GuardResult("blocked_label", "pass", "no blocked label")` on pass

**Circuit breaker clock change**: Replace `time.monotonic()` with `time.time()` in `_circuit_breaker_state` so audit detail can report ISO timestamps.

#### 1c. Refactor composite guard methods

`_check_pr_comment_guards` and `_check_issue_comment_guards` — collect `GuardResult` objects from each passing guard, short-circuit on first failure (return skip reason as before). On full pass, return the list of `GuardResult` objects.

#### 1d. Emit `guard_checked` event

After guards pass, before QueueItem creation (three insertion points: pr_comment ~line 278, issue_comment ~line 300, issue ~line 323):

```python
# Flatten guard results into event fields
guard_fields = {}
for gr in guard_results:
    guard_fields[f"guard_{gr.name}_result"] = gr.result
    guard_fields[f"guard_{gr.name}_detail"] = gr.detail

self.events.emit(
    action="guard_checked",
    repo=repo,
    event_type=event_type,
    number=number,
    pr_number=payload.get("pr_number"),
    comment_author=payload.get("comment_author", "unknown"),
    **guard_fields,
)
```

#### 1e. Add to VALID_ACTIONS

`receiver/dispatcher.py` — add `"guard_checked"` and `"receiver_started"` to the `VALID_ACTIONS` frozenset.

#### 1f. Update tests

- `tests/test_server.py` — verify guard methods return `GuardResult` on pass, `str` on fail
- `tests/test_server.py` — verify `guard_checked` event is emitted when all guards pass
- `tests/test_server.py` — verify guard detail strings contain no user content
- `tests/test_dispatcher.py` — update `TestValidActions` to include new actions

### Phase 2: Receiver — `pr_number` Propagation

**Files:** `receiver/queue.py`, `receiver/server.py`, `receiver/dispatcher.py`

#### 2a. Add `pr_number` to QueueItem

`receiver/queue.py` — add field with default:

```python
@dataclass
class QueueItem:
    type: str
    number: int
    # ... existing fields ...
    pr_number: int | None = None  # PR number when known (pr_comment, issue_comment on PR)
```

#### 2b. Set `pr_number` at ingestion

`receiver/server.py` — when creating QueueItem for pr_comment and issue_comment events, set `pr_number=payload.get("pr_number")`.

#### 2c. Propagate through dispatcher events

`receiver/dispatcher.py` — add `pr_number=item.pr_number` to all event emissions: triage, dispatched, spawned, done, cost_tracked, blocked.

#### 2d. Emit `receiver_started` event

`receiver/__main__.py` — after initialization, emit:

```python
events.emit(action="receiver_started", detail="circuit_breaker_state_reset")
```

### Phase 3: Dashboard — New Panels

**Repo:** `claude-agent-dashboard`
**Files:** `provisioning/dashboards/agent-dashboard.json`, optionally `promtail-config.yml`

#### 3a. Add `$number` template variable

Repo Detail dashboard (`agent-dashboard.json`) — add a text-input template variable `$number` with label "Issue/PR Number".

#### 3b. PR Timeline panel

New Logs panel in Repo Detail dashboard. LogQL:

```logql
{job="claude-agent", repo=~"$repo"} | json | (number = "$number" or pr_number = "$number") | line_format "{{.ts}} [{{.action}}] model={{.model}} pr={{.pr_number}} {{.detail}}"
```

Positioned in a new "PR Investigation" row. When `$number` is empty, show text: "Enter an issue or PR number above to view its event timeline."

#### 3c. Guard Audit panel

New Table panel in the same row. LogQL:

```logql
{job="claude-agent", action="guard_checked", repo=~"$repo"} | json | line_format "{{.ts}} #{{.number}} author={{.comment_author}} marker={{.guard_self_reply_marker_result}} sig={{.guard_self_reply_signature_result}} cb={{.guard_circuit_breaker_result}} state={{.guard_state_check_result}} blocked={{.guard_blocked_label_result}}"
```

Filterable by `$number` when set. Columns: timestamp, number, comment_author, each guard result, each guard detail.

#### 3d. Optionally extract `pr_number` as Promtail label

If query performance is slow, add `pr_number` to Promtail's JSON field extraction in `promtail-config.yml`. Start without it — Loki's `| json` pipeline should handle the filtering.

## Success Metrics

- Can filter dashboard to a specific PR and see the complete event timeline
- Can identify when the same PR was dispatched multiple times and see the guard audit for each dispatch
- Can distinguish "circuit breaker expired" from "receiver restarted" via `receiver_started` event
- Can see `comment_author` on pass-through events to spot agent self-replies that bypassed guards

## Dependencies & Risks

| Risk | Mitigation |
|---|---|
| QueueItem field addition breaks existing queue files | Default `None` ensures backward-compatible deserialization |
| `time.time()` clock skew in circuit breaker | Acceptable for audit logging; clock adjustments are rare on servers |
| JSONL file grows faster with guard_checked events | One extra event per accepted webhook — negligible vs existing volume |
| Guard detail strings leak user content | Explicitly use only metadata (author, state, counts), never comment body |

## Sources & References

- **Origin brainstorm:** [docs/brainstorms/2026-03-24-agent-refire-visibility-brainstorm.md](docs/brainstorms/2026-03-24-agent-refire-visibility-brainstorm.md) — Key decisions: enrich JSONL (not separate logs), two-tier guard verbosity, `pr_number` for correlation
- **Guard system:** `receiver/server.py:124-184` (guard functions), `server.py:378-438` (composite guard methods)
- **Event logging:** `receiver/dispatcher.py:29-79` (VALID_ACTIONS, EventLogger)
- **QueueItem:** `receiver/queue.py:20-42`
- **Dashboard:** `claude-agent-dashboard` repo — `provisioning/dashboards/agent-dashboard.json`
- **Historical context:** 14 self-reply incident drove 3-layer defense; circuit breaker had two bugs (never called, key conflation) fixed in `docs/plans/2026-03-18-001-fix-issue-comment-handling-and-loop-prevention-plan.md`
