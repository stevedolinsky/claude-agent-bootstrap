# Brainstorm: Connect Worker Prompt Templates

**Date:** 2026-03-25
**Status:** Draft

## What We're Building

Connect the dispatcher's `_build_prompt()` to the existing prompt templates so workers get full context and instructions to post GitHub comments after completing work.

## The Problem

Workers complete tasks (60-85s, 3700+ output tokens) but never post comments on GitHub. Root cause: `_build_prompt()` has been a 3-line placeholder since the first commit — it sends only repo, task type, and number. The full templates (`orchestrator.md`, `pr-responder.md`, `issue-comment-responder.md`) exist with detailed instructions but are never loaded.

Two independent gaps:

1. **Data gap**: Webhook payload fields needed by templates (`comment_body`, `comment_author`, `pr_branch`, `labels`, `issue_state`) are available at webhook ingestion but NOT stored on QueueItem — lost before dispatch time.

2. **Template gap**: `_build_prompt()` doesn't load or render templates. No templating library in dependencies.

## Current Data Flow

```
Webhook payload → server.py (has comment_body, comment_author, labels, etc.)
    → QueueItem (only stores: type, number, priority, comment_id, title, body, pr_number)
        → dispatcher._build_prompt() (3-line stub)
            → worker (claude --print) — no context, no instructions
```

## Variable Availability Audit

| Variable | Template(s) | In QueueItem? | In Payload? | Gap |
|---|---|---|---|---|
| `{{repo}}` | all | no (passed separately) | yes | Pass through |
| `{{number}}` | orchestrator, issue-comment | yes | yes | None |
| `{{pr_number}}` | pr-responder | yes | yes | None |
| `{{title}}` | orchestrator | yes (issue only) | yes | None for issues |
| `{{body}}` | orchestrator | yes (issue only) | yes | None for issues |
| `{{comment_body}}` | pr-responder, issue-comment | NO | yes | CRITICAL — lost at enqueue |
| `{{comment_author}}` | pr-responder, issue-comment | NO | yes | CRITICAL — lost at enqueue |
| `{{pr_branch}}` | pr-responder | NO | NO | CRITICAL — must fetch from GitHub API |
| `{{verify_chain}}` | all | NO | NO | Must come from config or CLAUDE.md |
| `{{events_file}}` | orchestrator | NO (in config) | no | Available via config |
| `{{branch}}` | orchestrator | NO | no | Auto-generate: `claude/issue-{{number}}` |
| `{{labels}}` | issue-comment | NO | yes | Lost at enqueue |
| `{{issue_state}}` | issue-comment | NO | yes | Lost at enqueue |
| `{{issue_title}}` | issue-comment | NO | no | Must fetch from GitHub API |
| `{{issue_body}}` | issue-comment | NO | no | Must fetch from GitHub API |
| `{{is_continuation}}` | orchestrator | NO | no | Check plan file at dispatch |
| `{{plan_json}}` | orchestrator | NO | no | Read plan file at dispatch |
| `{{step_index}}` | orchestrator | NO | no | Parse from plan file |
| `{{step_name}}` | orchestrator | NO | no | Parse from plan file |
| `{{commit_log}}` | orchestrator | NO | no | Run git command at dispatch |

## Key Decisions

1. **Store payload data on QueueItem** — Add `comment_body`, `comment_author`, `labels`, `issue_state` fields to QueueItem with defaults. This is the simplest fix for the CRITICAL data gap. Backward-compatible with existing queue files (all have defaults).

2. **Fetch `pr_branch` at dispatch time** — Not in the webhook payload. Two options: (a) add it to the GHA workflow payload, or (b) fetch via MCP/GitHub API in `_build_prompt()`. Option (a) is simpler if we control the workflow template.

3. **Simple string replacement for templates** — The templates use `{{variable}}` syntax. Python's `str.replace()` or `re.sub()` is sufficient — no Handlebars library needed. The one conditional (`{{#if is_continuation}}`) can be handled with a simple Python if/else that includes or excludes the block.

4. **`verify_chain` from config or CLAUDE.md** — Add a `verify_chain` field to Config (loaded from TOML) or parse it from the target repo's CLAUDE.md. Config is simpler.

5. **Route to correct template based on `item.type`** — `issue` → `orchestrator.md`, `pr_comment` → `pr-responder.md`, `issue_comment` → `issue-comment-responder.md`.

6. **Worker working directory** — Currently workers run from `~/claude-agent-bootstrap/`. They should either (a) pass `--directory <target_repo>` to `claude`, or (b) the prompt should be self-contained enough that cwd doesn't matter. Option (a) requires knowing the target repo path; option (b) is more portable.

## Open Questions

1. **Where does `verify_chain` come from?** — Config TOML? Parsed from target repo CLAUDE.md? Hardcoded per-repo in receiver config?

2. **Should `pr_branch` be added to the GHA webhook payload or fetched at dispatch time?** — Adding to payload is simpler but requires updating the workflow template.

3. **Do we need `--directory` on the worker, or is a self-contained prompt sufficient?** — If the worker needs to read/write files in the target repo, it needs the right cwd. But if it only uses MCP tools (GitHub, Supabase), cwd doesn't matter.
