---
title: "feat: Worker target repo context via --directory and task-specific prompts"
type: feat
status: active
date: 2026-03-26
origin: docs/brainstorms/2026-03-25-worker-target-repo-context-brainstorm.md
---

# feat: Worker Target Repo Context

## Overview

Align worker execution with setup.sh's original intent: workers should run in the context of the target repo, loading its CLAUDE.md for behavioral instructions and `.claude/settings.json` for tool permissions. `_build_prompt()` provides only the task-specific context (comment body, author, PR number).

## Problem Statement / Motivation

Workers run `claude --print` from `~/claude-agent-bootstrap/` and inherit that directory's (empty) CLAUDE.md. But `setup.sh` generates a full CLAUDE.md with agent instructions in the target repo (e.g., `~/permitradar/`). Workers never see it.

This caused a production regression on 2026-03-24: the bootstrap repo's local CLAUDE.md was wiped by `git reset origin/main`, and workers stopped posting comments entirely. The root cause is a structural mismatch — the receiver is centralized but workers lack per-repo context.

(see brainstorm: docs/brainstorms/2026-03-25-worker-target-repo-context-brainstorm.md)

## Proposed Solution

Three changes, each independently valuable:

1. **Repo path registry** — `setup.sh` registers the target repo's local path in `~/.claude/agent-receiver.toml`. The receiver reads this at startup.

2. **`--directory` on worker command** — The dispatcher looks up the repo path from the registry and passes `--directory <path>` to `claude --print`. Workers load the target repo's CLAUDE.md and settings.

3. **Task-specific `_build_prompt()`** — Replace the 3-line stub with a prompt that includes `comment_body` and `comment_author` (stored on QueueItem from the webhook payload). No template rendering — just f-strings.

## Implementation Plan

### Phase 1: Store comment data on QueueItem

**Files:** `receiver/queue.py`, `receiver/server.py`, `tests/test_server.py`

Add two fields to QueueItem (backward-compatible defaults):

```python
# receiver/queue.py
@dataclass(slots=True)
class QueueItem:
    # ... existing fields ...
    comment_body: str = ""
    comment_author: str = ""
```

Set them at enqueue time in `server.py` for `pr_comment` and `issue_comment` events:

```python
# For pr_comment and issue_comment:
item = QueueItem(
    ...,
    comment_body=payload.get("comment_body", ""),
    comment_author=payload.get("comment_author", "unknown"),
)
```

### Phase 2: Repo path registry in TOML + setup.sh

**Files:** `receiver/server.py` (Config), `setup.sh`

Add `[repos]` section to Config, read from TOML:

```toml
# ~/.claude/agent-receiver.toml
[repos."stevedolinsky/permitradar"]
path = "/home/sdolinsky/permitradar"

[repos."stevedolinsky/hydrantmap"]
path = "/home/sdolinsky/hydrantmap"
```

Config reads this as a `dict[str, str]` mapping `repo_name → local_path`.

Update `setup.sh` to append the repo registration and restart the receiver:

```bash
# At end of setup.sh, after committing CLAUDE.md:
REPO_FULL="$(git remote get-url origin | sed 's|.*github.com[:/]||;s|\.git$||')"
REPO_PATH="$(pwd)"
# Append to TOML (idempotent — overwrites existing entry)
python3 -c "
import tomllib, tomli_w, pathlib
path = pathlib.Path.home() / '.claude' / 'agent-receiver.toml'
data = tomllib.loads(path.read_text()) if path.exists() else {}
data.setdefault('repos', {})['$REPO_FULL'] = {'path': '$REPO_PATH'}
path.write_text(tomli_w.dumps(data))
"
# Restart receiver (in-flight workers survive)
bash "${SCRIPT_DIR}/stop.sh" 2>/dev/null
bash "${SCRIPT_DIR}/start.sh"
```

**Note:** If `tomli_w` is not available, use a simpler approach — append a raw TOML block or use a JSON sidecar file (`~/.claude/agent-repos.json`).

### Phase 3: Pass --directory to workers

**Files:** `receiver/dispatcher.py`

In `_run_worker()`, look up the repo path and add `--directory`:

```python
def _run_worker(self, repo: str, item: QueueItem, model: str, ...):
    repo_path = self._config.repo_paths.get(repo)

    cmd = [
        "claude",
        "--print",
        "--output-format", "json",
        "--model", model,
        "--max-budget-usd", str(self._config.per_worker_budget_usd),
    ]
    if repo_path:
        cmd.extend(["--directory", repo_path])

    # ... rest of subprocess spawn
```

If `repo_path` is None (unregistered repo), the worker runs without `--directory` — degraded but functional.

### Phase 4: Replace _build_prompt() stub

**Files:** `receiver/dispatcher.py`

Replace the 3-line placeholder with task-specific prompts:

```python
def _build_prompt(self, repo: str, item: QueueItem) -> str:
    if item.type == "pr_comment":
        return (
            f"Respond to a PR comment on {repo} PR #{item.number}.\n"
            f"Comment by {item.comment_author}:\n"
            f"{item.comment_body}\n"
        )
    elif item.type == "issue_comment":
        return (
            f"Respond to an issue comment on {repo} issue #{item.number}.\n"
            f"Comment by {item.comment_author}:\n"
            f"{item.comment_body}\n"
        )
    elif item.type == "issue":
        return (
            f"Work on issue #{item.number} in {repo}.\n"
            f"Title: {item.title}\n"
            f"Body:\n{item.body}\n"
        )
    else:
        return (
            f"You are working on {repo}.\n"
            f"Task type: {item.type}\n"
            f"Issue/PR number: {item.number}\n"
        )
```

No template rendering, no new dependencies. CLAUDE.md (via `--directory`) carries the behavioral instructions — the prompt only provides what happened.

## Technical Considerations

- **Backward compatibility**: QueueItem defaults (`""`) ensure existing queue files deserialize safely
- **TOML writing**: `setup.sh` needs to write TOML. If `tomli_w` isn't available, fall back to JSON sidecar (`~/.claude/agent-repos.json`) or raw string append
- **Worker CWD vs --directory**: `--directory` sets Claude's project directory without changing the subprocess CWD. The worker can still read/write files in the target repo via Claude's file tools.
- **In-flight workers survive restart**: Workers use `start_new_session=True` (separate process group). Receiver restart doesn't kill them.

## Acceptance Criteria

- [x] QueueItem has `comment_body` and `comment_author` fields, set at enqueue time
- [x] `setup.sh` registers repo path in `~/.claude/agent-repos.json` (JSON sidecar) — decided against TOML for simplicity
- [x] Dispatcher passes `--directory <repo_path>` to worker when path is registered
- [x] Unregistered repos fall back gracefully (no `--directory`, worker still runs)
- [x] `_build_prompt()` includes comment body and author for comment events, title and body for issues
- [ ] Workers load the target repo's CLAUDE.md (verified by agent posting comments with `<!-- claude-agent -->` marker) — needs deploy verification
- [x] Existing tests pass (88/88); pre-existing teardown warnings only
- [ ] Multi-repo: registering a second repo and restarting works without disrupting in-flight workers — needs deploy verification

## Success Metrics

- Agents post comments on GitHub after completing work (the regression that triggered this)
- Comments include `<!-- claude-agent -->` marker (from target repo's CLAUDE.md)
- Workers use target repo's verify chain (visible in worker output)

## Dependencies & Risks

| Risk | Mitigation |
|---|---|
| `tomli_w` not in dependencies | Fall back to JSON sidecar or raw TOML append |
| Target repo path moves | Re-run `setup.sh` from new location — overwrites TOML entry |
| `--directory` flag behavior changes in Claude Code update | Test with current version; flag is documented in `claude --help` |
| QueueItem `comment_body` too large | Truncate to first 10K chars at enqueue time (guard against massive comments) |

## Sources & References

- **Origin brainstorm:** [docs/brainstorms/2026-03-25-worker-target-repo-context-brainstorm.md](docs/brainstorms/2026-03-25-worker-target-repo-context-brainstorm.md) — Key decisions: --directory for CLAUDE.md, _build_prompt for task context only, TOML registration by setup.sh, templates unnecessary
- **Worker spawn code:** `receiver/dispatcher.py:544-558` (_run_worker)
- **_build_prompt stub:** `receiver/dispatcher.py:591-597`
- **QueueItem:** `receiver/queue.py:20-32`
- **setup.sh:** lines 170-256 (CLAUDE.md generation, commit, push, receiver start)
- **Regression analysis:** CLAUDE.md wiped by git reset → workers lost instructions → no comments posted
