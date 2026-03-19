
## Agent Fleet Configuration

### Verify Chain
Run after every code change:
```
${VERIFY_CHAIN}
```

### Git Conventions
- Conventional commits: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`
- Branch naming: `claude/issue-<number>` for agent-created branches
- Never commit to `main` directly

### Comment Signature (SAFETY-CRITICAL)
Every GitHub comment you post MUST end with this exact line:
```
<!-- claude-agent -->
```
This prevents infinite self-reply loops. Omitting this has caused production incidents (14 self-replies on a single PR). This is NON-NEGOTIABLE.

### Labels
- `agent` — Issue is ready for agent work (set by human)
- `agent-wip` — Agent is actively working (set by agent, atomic replace)
- `agent-blocked` — Agent is stuck, needs human help (set by agent after 3 failures)

### GitHub Operations
Use MCP tools for ALL GitHub operations. Do NOT use `gh` CLI — it is not installed.
- `mcp__github__create_pull_request` for PR creation
- `mcp__github__issue_write` for label management
- `mcp__github__add_issue_comment` for commenting

### Event Logging
Append structured events to `${EVENTS_FILE}` using this format:
```bash
echo '{"ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","action":"ACTION","repo":"REPO","number":N,...}' >> ${EVENTS_FILE}
```

Valid actions: `plan_created`, `step_started`, `step_completed`, `pr_created`

### Epic Plan Files
Epic decomposition plans are stored at `${PLANS_DIR}/epic-<number>.json`.
The dispatcher owns plan file writes — workers emit events, dispatcher updates the plan.
