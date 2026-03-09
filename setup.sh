#!/usr/bin/env zsh
#
# Claude Agent Bootstrap v2 — drop into any repo, run once, paste loops.
# Usage: ~/claude-agent-bootstrap/setup.sh (run from repo root)
#
# v2 improvements (learned from permitradar + hydrantmap deployments):
#   - Loops create PRs (not just push branches)
#   - TODO Worker checks claude-wip issues for user feedback
#   - PR Comment Responder checks both inline + general comments
#   - Lint Guardian / Build Watchdog dedup: skip if identical open PR exists
#     (issue branches are always unique — dedup only for recurring maintenance)
#   - Epic branch support: big project issues get a parent branch, sub-tasks
#     PR against it, parent merges to main when complete
#   - MCP-first rule: use available MCP tools directly (Supabase, GitHub, etc.)
#     instead of generating scripts for the human to run
#   - Post-3-day renewal instructions included
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

# --- Detect project ---
REPO_NAME=$(basename "$REPO_ROOT")
GITHUB_USER=$(git remote get-url origin 2>/dev/null | sed -n 's|.*github.com[:/]\([^/]*\)/.*|\1|p')
GITHUB_REPO=$(git remote get-url origin 2>/dev/null | sed -n 's|.*github.com[:/][^/]*/\(.*\)\.git|\1|p')

if [[ -z "$GITHUB_REPO" ]]; then
  GITHUB_REPO=$(git remote get-url origin 2>/dev/null | sed -n 's|.*github.com[:/][^/]*/\(.*\)|\1|p')
fi

echo "📦 Project: $REPO_NAME"
echo "🔗 GitHub:  ${GITHUB_USER:-unknown}/${GITHUB_REPO:-unknown}"

# --- Detect package manager and source dir ---
PKG_DIR=""
PKG_MGR=""
if [[ -f "package.json" ]]; then
  PKG_DIR="."
elif [[ -f "web/package.json" ]]; then
  PKG_DIR="web"
elif [[ -f "app/package.json" ]]; then
  PKG_DIR="app"
elif [[ -f "frontend/package.json" ]]; then
  PKG_DIR="frontend"
fi

if [[ -n "$PKG_DIR" ]]; then
  if [[ -f "$PKG_DIR/bun.lockb" ]]; then
    PKG_MGR="bun"
  elif [[ -f "$PKG_DIR/pnpm-lock.yaml" ]]; then
    PKG_MGR="pnpm"
  elif [[ -f "$PKG_DIR/yarn.lock" ]]; then
    PKG_MGR="yarn"
  else
    PKG_MGR="npm"
  fi
fi

echo "📁 Package dir: ${PKG_DIR:-none}"
echo "📦 Package manager: ${PKG_MGR:-none}"

# --- Detect source directory ---
SRC_DIR=""
if [[ -n "$PKG_DIR" ]]; then
  if [[ -d "$PKG_DIR/src" ]]; then
    SRC_DIR="$PKG_DIR/src"
  elif [[ -d "$PKG_DIR/app" ]]; then
    SRC_DIR="$PKG_DIR/app"
  elif [[ -d "$PKG_DIR/lib" ]]; then
    SRC_DIR="$PKG_DIR/lib"
  else
    SRC_DIR="$PKG_DIR"
  fi
fi

echo "📂 Source dir: ${SRC_DIR:-none}"

# --- Detect available commands ---
INSTALL_CMD=""
LINT_CMD=""
BUILD_CMD=""
TEST_CMD=""

if [[ -n "$PKG_DIR" && -f "$PKG_DIR/package.json" ]]; then
  INSTALL_CMD="cd $PKG_DIR && $PKG_MGR install"
  [[ "$PKG_MGR" == "npm" ]] && INSTALL_CMD="cd $PKG_DIR && npm ci"

  SCRIPTS=$(python3 -c "import json; d=json.load(open('$PKG_DIR/package.json')); print(' '.join(d.get('scripts',{}).keys()))" 2>/dev/null || echo "")

  [[ "$SCRIPTS" == *"lint"* ]] && LINT_CMD="cd $PKG_DIR && $PKG_MGR run lint"
  [[ "$SCRIPTS" == *"build"* ]] && BUILD_CMD="cd $PKG_DIR && $PKG_MGR run build"
  [[ "$SCRIPTS" == *"test"* ]] && TEST_CMD="cd $PKG_DIR && $PKG_MGR test"

  # Check for vitest even if no test script yet
  if [[ -z "$TEST_CMD" ]]; then
    if grep -q "vitest" "$PKG_DIR/package.json" 2>/dev/null; then
      TEST_CMD="cd $PKG_DIR && npx vitest run"
    fi
  fi
fi

echo "🔧 Commands:"
echo "   install: ${INSTALL_CMD:-none}"
echo "   lint:    ${LINT_CMD:-none}"
echo "   build:   ${BUILD_CMD:-none}"
echo "   test:    ${TEST_CMD:-none}"

# --- Build verify chain ---
VERIFY_CHAIN=""
[[ -n "$INSTALL_CMD" ]] && VERIFY_CHAIN="$INSTALL_CMD"
[[ -n "$LINT_CMD" ]] && VERIFY_CHAIN="${VERIFY_CHAIN:+$VERIFY_CHAIN && }$LINT_CMD"
[[ -n "$BUILD_CMD" ]] && VERIFY_CHAIN="${VERIFY_CHAIN:+$VERIFY_CHAIN && }$BUILD_CMD"
[[ -n "$TEST_CMD" ]] && VERIFY_CHAIN="${VERIFY_CHAIN:+$VERIFY_CHAIN && }$TEST_CMD"

# --- Create .claude/settings.json ---
mkdir -p .claude
cat > .claude/settings.json << 'SETTINGS_EOF'
{
  "permissions": {
    "allow": [
      "Bash",
      "mcp__github__*",
      "Edit",
      "Read",
      "Write",
      "Glob",
      "Grep"
    ],
    "deny": [
      "Bash(rm -rf*)",
      "Bash(npm publish*)"
    ]
  }
}
SETTINGS_EOF
echo "✅ Created .claude/settings.json"

# --- Create CI workflow if not exists ---
if [[ ! -f ".github/workflows/ci.yml" ]] && [[ -n "$PKG_DIR" ]]; then
  mkdir -p .github/workflows

  # Detect lock file for caching
  CACHE_MGR="npm"
  CACHE_DEP_PATH="$PKG_DIR/package-lock.json"
  if [[ "$PKG_MGR" == "yarn" ]]; then
    CACHE_MGR="yarn"
    CACHE_DEP_PATH="$PKG_DIR/yarn.lock"
  elif [[ "$PKG_MGR" == "pnpm" ]]; then
    CACHE_MGR="pnpm"
    CACHE_DEP_PATH="$PKG_DIR/pnpm-lock.yaml"
  elif [[ "$PKG_MGR" == "bun" ]]; then
    CACHE_MGR=""
    CACHE_DEP_PATH=""
  fi

  # Detect if Next.js (needs placeholder env vars for static build)
  IS_NEXTJS=""
  if grep -q '"next"' "$PKG_DIR/package.json" 2>/dev/null; then
    IS_NEXTJS="true"
  fi

  # Build CI steps
  CI_STEPS=""
  if [[ -n "$INSTALL_CMD" ]]; then
    CI_STEPS="${CI_STEPS}      - run: $INSTALL_CMD
"
  fi
  if [[ -n "$LINT_CMD" ]]; then
    CI_STEPS="${CI_STEPS}      - run: $LINT_CMD
"
  fi
  if [[ -n "$BUILD_CMD" ]]; then
    if [[ -n "$IS_NEXTJS" ]]; then
      CI_STEPS="${CI_STEPS}      - run: $BUILD_CMD
        env:
          NEXT_PUBLIC_SUPABASE_URL: https://placeholder.supabase.co
          NEXT_PUBLIC_SUPABASE_ANON_KEY: placeholder
"
    else
      CI_STEPS="${CI_STEPS}      - run: $BUILD_CMD
"
    fi
  fi
  if [[ -n "$TEST_CMD" ]]; then
    CI_STEPS="${CI_STEPS}      - name: Run tests
        run: $TEST_CMD
        continue-on-error: true
"
  else
    # Add vitest with passWithNoTests as fallback
    CI_STEPS="${CI_STEPS}      - name: Run tests
        run: cd $PKG_DIR && npx vitest run --passWithNoTests
        continue-on-error: true
"
  fi

  # Write the workflow
  cat > .github/workflows/ci.yml << CI_EOF
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: ci-\${{ github.ref }}
  cancel-in-progress: true

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
$(if [[ -n "$CACHE_MGR" ]]; then
echo "          cache: $CACHE_MGR"
echo "          cache-dependency-path: $CACHE_DEP_PATH"
fi)
$CI_STEPS
CI_EOF
  echo "✅ Created .github/workflows/ci.yml"
else
  if [[ -f ".github/workflows/ci.yml" ]]; then
    echo "⏭️  .github/workflows/ci.yml already exists, skipping"
  fi
fi

# --- Append agent instructions to CLAUDE.md ---
# If CLAUDE.md exists, append. If not, create with a header first.
if [[ ! -f "CLAUDE.md" ]]; then
  echo "# $REPO_NAME" > CLAUDE.md
  echo "" >> CLAUDE.md
  echo "✅ Created CLAUDE.md"
else
  echo "" >> CLAUDE.md
  echo "📝 Appending agent instructions to existing CLAUDE.md"
fi

# Check if agent instructions were already appended (idempotent)
if grep -q "## Autonomous Loop Rules" CLAUDE.md 2>/dev/null; then
  echo "⏭️  Agent instructions already present in CLAUDE.md, skipping"
else
  cat >> CLAUDE.md << CLAUDE_EOF

# $REPO_NAME — Agent Instructions

## Project
$(if [[ -n "$PKG_DIR" ]]; then echo "Package directory: \`$PKG_DIR/\`. Source: \`$SRC_DIR/\`."; else echo "Detected at \`$REPO_ROOT\`."; fi)

## Commands (always run from \`$PKG_DIR/\` directory)
$(if [[ -n "$INSTALL_CMD" ]]; then echo "- Install: \`$INSTALL_CMD\`"; fi)
$(if [[ -n "$LINT_CMD" ]]; then echo "- Lint: \`$LINT_CMD\`"; fi)
$(if [[ -n "$BUILD_CMD" ]]; then echo "- Build: \`$BUILD_CMD\`"; fi)
$(if [[ -n "$TEST_CMD" ]]; then echo "- Test: \`$TEST_CMD\`"; fi)

## Git Conventions
- Conventional commits: \`feat:\`, \`fix:\`, \`chore:\`, \`test:\`, \`refactor:\`
- Feature branches: \`claude/<description>-<id>\`
- Always push with \`-u origin <branch>\`
- Never force push. Never merge/rebase main without human approval.

## Autonomous Loop Rules
- Use git worktrees for isolation: \`git worktree add /tmp/$REPO_NAME-<task> -b claude/<task>-<id>\`
- After every change: run lint → build → test (if available) → only commit if all pass
- One logical change per commit. Keep changes narrow and independent.
- If 3+ consecutive failures on the same issue, stop and report.
- Clean up worktrees when done: \`git worktree remove /tmp/$REPO_NAME-<task>\`
- **Maintenance dedup** (Lint Guardian, Build Watchdog only): before creating a new lint-fix or build-fix branch, check if an identical open PR already exists. If so, skip. Issue branches are always unique — every issue gets its own branch.

## Epic / Project Branch Rules
- If an issue describes a **large project** with multiple sub-tasks (e.g., "Plan Gloucester County expansion"), create a parent epic branch: \`claude/epic-<name>\`
- Sub-task branches are created from the epic branch (not main): \`git worktree add /tmp/$REPO_NAME-<subtask> -b claude/<subtask> claude/epic-<name>\`
- Sub-task PRs target the epic branch as base (not main)
- When all sub-tasks are merged into the epic branch, create a single PR from epic branch → main for human review
- Keep each sub-task's changes isolated — never combine unrelated issues into the same branch or PR

## Agent Backlog Convention
- **GitHub Issues** with label \`claude-ready\` are the primary work queue.
- Claim issues by: removing \`claude-ready\`, adding \`claude-wip\`, self-assigning.
- When done: create a PR with "Closes #N" in the body, comment on the issue with PR link, remove \`claude-wip\`.
- If blocked: add \`claude-blocked\` label, comment explaining the problem.
- **Inline TODOs**: \`// TODO(@claude): <description>\` in source files are micro-tasks.
- Regular \`// TODO:\` comments (without @claude) are NOT agent work items — leave them alone.
- When completing an inline TODO(@claude): remove the comment entirely after implementing.
- One item per iteration. Never pick up multiple items at once.

## Model Routing
- Issues labeled \`claude-sonnet\` are handled by the Sonnet session (fast, cheap). Issues labeled \`claude-opus\` are handled by the Opus session (powerful, thorough).
- The **Triage Worker** (runs on Sonnet) classifies incoming \`claude-ready\` issues automatically:
  - **Sonnet tasks**: typo fixes, copy changes, simple bug fixes with clear steps, dependency bumps, config tweaks, lint/style fixes, adding tests for existing code, small UI changes, one-file changes
  - **Opus tasks**: new features requiring multi-file changes, architectural decisions, complex refactors, epic/project issues with sub-tasks, performance optimization, security fixes, anything requiring deep codebase understanding
- If unsure, default to \`claude-opus\` — it's better to over-qualify than under-deliver.
- Inline \`// TODO(@claude):\` micro-tasks default to Sonnet unless the comment indicates complexity.
- Maintenance loops (Lint Guardian, Build Watchdog) always run on Sonnet.
- Code Quality Sweep always runs on Opus.

## MCP-First Rule
- **Always use available MCP tools directly** instead of generating scripts for the human to run.
- If you have mcp__supabase__execute_sql, run the SQL yourself — do not write a .sql file and say "go run this".
- If you have mcp__github__create_pull_request, create the PR yourself — do not tell the human to do it.
- The human should only be involved for decisions and approvals, never for executing steps the agent can do itself.
- Check your available tools before suggesting manual steps. If a tool exists for the action, use it.

## Verify Chain
Run this after every change before committing:
\`\`\`
${VERIFY_CHAIN:-# no verify commands detected}
\`\`\`

## Code Patterns
$(if [[ -n "$PKG_DIR" ]]; then
echo "- Source directory: \`$SRC_DIR/\`"
if [[ -d "$SRC_DIR/lib" ]]; then echo "- Shared utilities: \`$SRC_DIR/lib/\`"; fi
if [[ -d "$SRC_DIR/components" ]]; then echo "- Components: \`$SRC_DIR/components/\`"; fi
if [[ -d "$SRC_DIR/app" ]]; then echo "- App Router pages: \`$SRC_DIR/app/\`"; fi
if [[ -d "$SRC_DIR/pages" ]]; then echo "- Pages: \`$SRC_DIR/pages/\`"; fi
if [[ -f "$SRC_DIR/lib/types.ts" ]]; then echo "- Types: \`$SRC_DIR/lib/types.ts\`"; fi
else
echo "- Source detected at \`$REPO_ROOT\`"
fi)
CLAUDE_EOF
  echo "✅ Appended agent instructions to CLAUDE.md"
fi

# --- Generate LOOPS.md ---
cat > LOOPS.md << LOOPS_EOF
# $REPO_NAME — Autonomous Loops

Generated by \`claude-agent-bootstrap/setup.sh\` v3 for **${GITHUB_USER}/${GITHUB_REPO}**.

## Quick Start

\`\`\`bash
./start-loops.sh          # start both sessions (7 loops)
./start-loops.sh sonnet   # start only Sonnet (5 loops)
./start-loops.sh opus     # start only Opus (2 loops)
./start-loops.sh stop     # kill both sessions
\`\`\`

Monitor: \`tmux attach -t claude-sonnet\` / \`tmux attach -t claude-opus\`

## Model Routing

This setup uses **two Claude Code sessions** running in parallel — one with Sonnet (fast/cheap)
for straightforward work, and one with Opus (powerful) for complex tasks. A Triage Worker
automatically classifies incoming issues so work flows to the right model.

| Session | Model | Loops |
|---------|-------|-------|
| **Sonnet** | claude-sonnet-4-6 | Triage Worker, Sonnet TODO Worker, Lint Guardian, Build Watchdog, PR Comment Responder |
| **Opus** | claude-opus-4-6 | Opus TODO Worker, Code Quality Sweep |

**How it works:**
1. You file an issue with \`claude-ready\` label (as before)
2. The **Triage Worker** (Sonnet) reads the issue, assesses complexity, and relabels it \`claude-sonnet\` or \`claude-opus\`
3. The appropriate TODO Worker picks it up based on label
4. If you already know the complexity, skip triage by labeling \`claude-sonnet\` or \`claude-opus\` directly

## One-Shot Setup (run these first, once)

Paste these one at a time in **either** session before starting the recurring loops.

\`\`\`
You are a Test Bootstrapper for ${GITHUB_USER}/${GITHUB_REPO}. This is a one-shot task:
1) Create a worktree from origin/main: git worktree add /tmp/${GITHUB_REPO}-test-bootstrap -b claude/add-test-infra main
2) cd into the worktree's package dir ($PKG_DIR)
3) Install test dependencies: npm install -D vitest @vitejs/plugin-react @testing-library/react @testing-library/jest-dom jsdom
4) Create vitest.config.ts with: react plugin, jsdom environment, setupFiles pointing to test-setup.ts, path alias @ -> ./src
5) Create ${SRC_DIR:-src}/test-setup.ts with: import "@testing-library/jest-dom/vitest"
6) Write initial tests for any utility functions found in ${SRC_DIR:-src}/lib/ and at least one component test in ${SRC_DIR:-src}/components/
7) Run: ${VERIFY_CHAIN:-npm run lint && npm run build && npx vitest run}
8) If all green: commit "chore: add vitest test infrastructure with initial tests", push -u origin claude/add-test-infra
9) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: claude/add-test-infra, base: main)
10) Clean up worktree: git worktree remove /tmp/${GITHUB_REPO}-test-bootstrap
\`\`\`

\`\`\`
You are a Type Strictifier for ${GITHUB_USER}/${GITHUB_REPO}. This is a one-shot task:
1) Create a worktree from origin/main: git worktree add /tmp/${GITHUB_REPO}-type-strict -b claude/remove-any-types main
2) Search all .ts and .tsx files in ${SRC_DIR:-src}/ for explicit \`any\` types (grep for ": any" and "<any>" and "as any")
3) Replace each \`any\` with the correct specific type. Use \`unknown\` only when the type genuinely cannot be determined. Prefer interfaces from the project's existing type definitions.
4) Run: ${VERIFY_CHAIN:-npm run lint && npm run build && npx vitest run}
5) If all green: commit "fix: replace any types with strict alternatives", push -u origin claude/remove-any-types
6) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: claude/remove-any-types, base: main)
7) Clean up worktree: git worktree remove /tmp/${GITHUB_REPO}-type-strict
\`\`\`

---

## Session 1: Sonnet (fast/cheap) — \`claude --model claude-sonnet-4-6\`

Paste all 5 of these into a Sonnet session:

### Triage Worker
\`\`\`
/loop 2m You are a Triage Worker loop for ${GITHUB_USER}/${GITHUB_REPO}. On each iteration: 1) Check GitHub Issues labeled \`claude-ready\` using mcp__github__list_issues (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, labels: ["claude-ready"]). 2) If none found, stop. 3) For each \`claude-ready\` issue (max 5 per iteration), read the title and body and assess complexity: SONNET tasks = typo fixes, copy/text changes, simple bug fixes with clear reproduction steps, dependency bumps, config/env tweaks, lint/style fixes, adding tests for existing code, small UI changes (color, spacing, text), single-file changes, documentation updates, adding error messages, renaming variables/functions. OPUS tasks = new features requiring 3+ file changes, architectural decisions or restructuring, complex refactors spanning multiple modules, epic/project issues with sub-tasks, performance optimization requiring profiling, security fixes, database schema changes, API design, anything requiring deep codebase understanding or creative problem-solving, issues where the approach is ambiguous. 4) Relabel the issue: remove \`claude-ready\`, add \`claude-sonnet\` or \`claude-opus\` via mcp__github__issue_write. 5) Add a brief comment explaining the classification via mcp__github__add_issue_comment, e.g. "Triaged as sonnet-level: single-file config change" or "Triaged as opus-level: multi-file feature requiring new component architecture". 6) If unsure, default to \`claude-opus\` — better to over-qualify than under-deliver. 7) Never pick up work yourself — only classify and route.
\`\`\`

### Sonnet TODO Worker
\`\`\`
/loop 5m You are a Sonnet TODO Worker loop for ${GITHUB_USER}/${GITHUB_REPO}. You handle SIMPLE tasks only. On each iteration: 1) Check GitHub Issues labeled \`claude-sonnet\` using mcp__github__list_issues (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, labels: ["claude-sonnet"]). 2) ALSO check issues labeled \`claude-wip\` that were originally \`claude-sonnet\` for new user comments — use mcp__github__issue_read with method "get_comments" to see if the user has replied with decisions, approvals, or feedback. 3) Scan for inline \`// TODO(@claude):\` comments in ${SRC_DIR:-src}/ — these default to Sonnet unless the comment indicates complex work. 4) If nothing found and no new user comments on wip issues, stop. 5) Pick ONE item (prefer issues over TODOs). 6) For issues: relabel claude-sonnet→claude-wip via mcp__github__issue_write. 7) Check if the issue is a sub-task of a larger epic. If yes: create worktree from the epic branch (claude/epic-<name>), PR against that branch. If no: create worktree from origin/main, PR against main. 8) Implement the change, run ${VERIFY_CHAIN:-tests}, commit+push. 9) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: branch, base: epic-branch-or-main) with title from the issue and body that says "Closes #N" plus a summary. 10) Comment on the issue with the PR link via mcp__github__add_issue_comment. 11) Clean up worktree. If stuck after 3 attempts: add \`claude-blocked\` label and add comment "Blocked — may need opus-level reasoning. Consider relabeling to claude-opus." 12) For inline TODOs: same flow — implement, remove the comment, commit+push, create PR. 13) For wip issues with new user comments: incorporate feedback, reply acknowledging. 14) IMPORTANT: Always use available MCP tools directly. One item per iteration. Never force push.
\`\`\`

### Lint Guardian
\`\`\`
/loop 30m You are a Lint Guardian loop for ${GITHUB_USER}/${GITHUB_REPO}. On each iteration: 1) FIRST check if there's already an open PR with "lint" in the title that YOU created (author is the bot) via mcp__github__list_pull_requests (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, state: open). If an unmerged lint-fix PR already exists, skip this iteration — wait for it to be merged. 2) Create worktree from origin/main. 3) ${INSTALL_CMD:-npm ci} && ${LINT_CMD:+${LINT_CMD} --fix ||} npx eslint ${SRC_DIR:-src}/ --fix. 4) If no file changes (git diff --stat is empty), clean up and stop — lint is clean. 5) If changes: commit "fix: auto-fix lint violations", verify ${VERIFY_CHAIN:-lint+build+test pass}, push branch. 6) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: branch, base: main) with title "fix: auto-fix lint violations" and body summarizing what was fixed. 7) Clean up worktree. Never force push.
\`\`\`

### Build Watchdog
\`\`\`
/loop 30m You are a Build Watchdog loop for ${GITHUB_USER}/${GITHUB_REPO}. On each iteration: 1) FIRST check if there's already an open PR with "build" or "type error" in the title that YOU created via mcp__github__list_pull_requests (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, state: open). If an unmerged build-fix PR already exists, skip this iteration — wait for it to be merged. 2) Create worktree from origin/main. 3) ${INSTALL_CMD:-npm ci}. 4) Run ${BUILD_CMD:-npm run build}. 5) If build succeeds with zero errors, clean up and stop — build is green. 6) If type errors or compilation failures: fix them, verify ${VERIFY_CHAIN:-lint+build+test}, commit+push. 7) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: branch, base: main) with title "fix: resolve build errors" and body describing what was fixed. 8) Clean up worktree. If stuck after 3 attempts, stop and report. Never force push.
\`\`\`

### PR Comment Responder
\`\`\`
/loop 5m You are a PR Comment Responder loop for ${GITHUB_USER}/${GITHUB_REPO}. On each iteration: 1) List open PRs via mcp__github__list_pull_requests (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}). 2) Check BOTH inline review comments (get_review_comments) AND general PR comments (get_comments) via mcp__github__pull_request_read. 3) Filter for actionable requests — skip approvals, acks, bot messages. If none, stop. 4) Assess each comment's complexity: if the requested change is a simple fix (typo, rename, small tweak, adding a check), handle it. If the comment requests a complex rework (redesign component, rethink approach, add substantial new logic), reply acknowledging and add a comment: "This requires deeper changes — escalating to opus session" and add label \`claude-opus\` to the PR. 5) For changes you handle (max 3): create worktree from PR branch, make the requested change, verify ${VERIFY_CHAIN:-lint+build+test}, commit+push to the PR branch. 6) For inline comments: reply via mcp__github__add_reply_to_pull_request_comment. For general comments: reply via mcp__github__add_issue_comment. Body: "Addressed in latest commit." 7) If the comment asks you to run something (SQL, deploy, etc.) and you have an MCP tool for it, execute it directly. 8) Clean up worktree. Never force push.
\`\`\`

---

## Session 2: Opus (powerful) — \`claude --model claude-opus-4-6\`

Paste both of these into an Opus session:

### Opus TODO Worker
\`\`\`
/loop 5m You are an Opus TODO Worker loop for ${GITHUB_USER}/${GITHUB_REPO}. You handle COMPLEX tasks that require deep reasoning. On each iteration: 1) Check GitHub Issues labeled \`claude-opus\` using mcp__github__list_issues (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, labels: ["claude-opus"]). 2) ALSO check issues labeled \`claude-wip\` that were originally \`claude-opus\` for new user comments — use mcp__github__issue_read with method "get_comments". 3) ALSO check open PRs labeled \`claude-opus\` (escalated from Sonnet PR Comment Responder) via mcp__github__list_pull_requests — these are PRs needing complex rework based on review feedback. 4) If nothing found, stop. 5) Pick ONE item (prefer escalated PRs > issues > inline TODOs). 6) For issues: relabel claude-opus→claude-wip via mcp__github__issue_write. 7) Check if the issue is an epic (describes a large project with sub-tasks). If yes: create the epic branch \`claude/epic-<name>\`, break down into sub-issues (label each \`claude-opus\` or \`claude-sonnet\` based on sub-task complexity), and create them via mcp__github__issue_write. 8) For regular issues: check if it's a sub-task of a larger epic. If yes: create worktree from the epic branch, PR against that branch. If no: create worktree from origin/main, PR against main. 9) Implement the change, run ${VERIFY_CHAIN:-tests}, commit+push. 10) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: branch, base: epic-branch-or-main) with title from the issue and body that says "Closes #N" plus a thorough summary of the approach and changes. 11) Comment on the issue with the PR link via mcp__github__add_issue_comment. 12) For escalated PRs: create worktree from the PR branch, address the complex review feedback, verify, commit+push, reply to the comments. 13) Clean up worktree. If stuck after 3 attempts: add \`claude-blocked\` label. 14) IMPORTANT: Always use available MCP tools directly. One item per iteration. Never force push.
\`\`\`

### Code Quality Sweep
\`\`\`
/loop 1h You are a Code Quality Sweep loop for ${GITHUB_USER}/${GITHUB_REPO}. On each iteration: 1) FIRST check if there's already an open PR with "refactor:" or "quality" in the title via mcp__github__list_pull_requests (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, state: open). If 3+ quality PRs are open, skip this iteration — let the human merge before creating more. 2) Create worktree from origin/main. 3) Search for: TODO/FIXME/HACK comments (not @claude), unused exports, functions >80 lines, duplicated code blocks. 4) Fix exactly ONE issue. 5) Verify ${VERIFY_CHAIN:-lint+build+test}. 6) Commit+push. 7) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: branch, base: main) with title "refactor: <summary>" and body describing the quality issue fixed. 8) Clean up worktree. 9) If nothing found, stop — codebase is clean. One issue per iteration. Max 2 files per commit. Never force push.
\`\`\`

---

## Labels Required

Create these on your GitHub repo:
- \`claude-ready\` — add to any issue for agent pickup (Triage Worker will classify)
- \`claude-sonnet\` — simple tasks routed to Sonnet session
- \`claude-opus\` — complex tasks routed to Opus session
- \`claude-wip\` — agent adds when claiming
- \`claude-blocked\` — agent adds if stuck

**Shortcut:** Skip triage by labeling issues \`claude-sonnet\` or \`claude-opus\` directly instead of \`claude-ready\`.

## Filing Work

**GitHub Issues** (features, bugs):
\`\`\`
Title: feat: add dark mode toggle
Labels: claude-ready     (or claude-sonnet / claude-opus if you know the complexity)
Body: Add a dark mode toggle to the navbar. Store preference in localStorage.
Files: src/components/Navbar.tsx, src/styles/globals.css
\`\`\`

**Epic / project issues** (multi-step features — always Opus):
\`\`\`
Title: Plan: add multi-county support
Labels: claude-opus      (epics always go to Opus — skip triage)
Body: Plan and implement multi-county support. Break this into sub-tasks as
separate issues, create an epic branch, and PR sub-tasks against it.
Do not merge to main until all sub-tasks are complete and I approve.
\`\`\`
The Opus agent will create \`claude/epic-multi-county\`, file sub-issues
(labeling each \`claude-sonnet\` or \`claude-opus\` based on sub-task complexity),
and PR each sub-task against the epic branch.

**Inline micro-tasks** (while coding — default to Sonnet):
\`\`\`typescript
// TODO(@claude): memoize this expensive filter
// TODO(@claude): add aria-label to this button
\`\`\`

## After the Initial 3-Day Period

When your first loop session expires (Claude Code sessions last ~3 days max),
here's how to transition to steady-state maintenance.

### 1. Review what was accomplished

Run these in a new Claude Code session to assess the first run:

\`\`\`
Review the state of ${GITHUB_USER}/${GITHUB_REPO} after the initial bootstrap period:
1) List all open PRs via mcp__github__list_pull_requests — how many need merge/close?
2) List issues still labeled claude-wip or claude-blocked — any stuck work?
3) Count branches: git branch | grep claude/ | wc -l — how much cleanup needed?
4) Run the verify chain to confirm main is green: ${VERIFY_CHAIN:-npm run build}
5) Summarize: PRs to merge, issues to close, branches to prune, any remaining work.
\`\`\`

### 2. Clean up stale branches

\`\`\`
Clean up stale agent branches in ${GITHUB_USER}/${GITHUB_REPO}:
1) List all local branches matching claude/*: git branch | grep claude/
2) For each: check if it has a merged or closed PR. If yes, delete: git branch -d claude/<name>
3) Prune remote tracking refs: git fetch --prune
4) List any worktrees still dangling: git worktree list
5) Remove orphaned worktrees: git worktree prune
6) Report how many branches were cleaned up.
\`\`\`

### 3. Decide which loops to keep

After the bootstrap phase, you may reduce the fleet:

| Loop | Session | Keep running? | Why |
|------|---------|--------------|-----|
| Triage Worker | Sonnet | **Yes** — routes work automatically | Needed as long as you use \`claude-ready\` label |
| Sonnet TODO Worker | Sonnet | **Yes** — core work engine for simple tasks | Picks up sonnet-level issues |
| Opus TODO Worker | Opus | **Yes** — core work engine for complex tasks | Picks up opus-level issues |
| PR Comment Responder | Sonnet | **Yes** — essential for feedback loop | Responds to your PR reviews |
| Lint Guardian | Sonnet | **Maybe** — drop if lint is clean for 3+ cycles | Only needed if lint issues keep appearing |
| Build Watchdog | Sonnet | **Maybe** — drop if build is stable | Only needed if build breaks regularly |
| Code Quality Sweep | Opus | **Drop or reduce to daily** | Aggressive in bootstrap; noisy in steady state |

### 4. Restart the loops you want

**Sonnet session** (\`claude --model claude-sonnet-4-6\`):

Reduce intervals if the codebase is stable:
- Triage Worker: keep at 2m (or increase to 5m if issue volume is low)
- Sonnet TODO Worker: keep at 5m (or increase to 15m)
- PR Comment Responder: keep at 5m
- Lint Guardian: increase to 1h or drop
- Build Watchdog: increase to 1h or drop

**Opus session** (\`claude --model claude-opus-4-6\`):
- Opus TODO Worker: keep at 5m (or increase to 15m)
- Code Quality Sweep: increase to 4h or drop

### 5. Ongoing workflow

Your steady-state loop is:
1. **File issues** with \`claude-ready\` label — Triage Worker routes them automatically
2. **Or label directly** with \`claude-sonnet\` / \`claude-opus\` to skip triage
3. **Add \`// TODO(@claude):\`** comments in code for micro-tasks (handled by Sonnet)
4. **Review PRs** the agents create — leave comments for changes
5. **Merge** when satisfied — the agents handle the rest
6. **Restart both sessions** when they expire (~3 days): \`./start-loops.sh\`

### Quick restart

\`\`\`bash
./start-loops.sh stop   # kill expired sessions
./start-loops.sh        # restart both (reads prompts from .claude/loops/)
\`\`\`

Or restart one at a time:
\`\`\`bash
./start-loops.sh sonnet   # 5 loops: triage, todo worker, lint, build, PR responder
./start-loops.sh opus     # 2 loops: todo worker, code quality sweep
\`\`\`

To adjust loop intervals, edit the prompt files in \`.claude/loops/\` (e.g., change \`/loop 30m\` to \`/loop 1h\`)
and restart the relevant session.
LOOPS_EOF
echo "✅ Created LOOPS.md"

# --- Write individual loop prompt files ---
mkdir -p .claude/loops

cat > .claude/loops/01-triage.txt << PROMPT_EOF
/loop 2m You are a Triage Worker loop for ${GITHUB_USER}/${GITHUB_REPO}. On each iteration: 1) Check GitHub Issues labeled \`claude-ready\` using mcp__github__list_issues (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, labels: ["claude-ready"]). 2) If none found, stop. 3) For each \`claude-ready\` issue (max 5 per iteration), read the title and body and assess complexity: SONNET tasks = typo fixes, copy/text changes, simple bug fixes with clear reproduction steps, dependency bumps, config/env tweaks, lint/style fixes, adding tests for existing code, small UI changes (color, spacing, text), single-file changes, documentation updates, adding error messages, renaming variables/functions. OPUS tasks = new features requiring 3+ file changes, architectural decisions or restructuring, complex refactors spanning multiple modules, epic/project issues with sub-tasks, performance optimization requiring profiling, security fixes, database schema changes, API design, anything requiring deep codebase understanding or creative problem-solving, issues where the approach is ambiguous. 4) Relabel the issue: remove \`claude-ready\`, add \`claude-sonnet\` or \`claude-opus\` via mcp__github__issue_write. 5) Add a brief comment explaining the classification via mcp__github__add_issue_comment, e.g. "Triaged as sonnet-level: single-file config change" or "Triaged as opus-level: multi-file feature requiring new component architecture". 6) If unsure, default to \`claude-opus\` — better to over-qualify than under-deliver. 7) Never pick up work yourself — only classify and route.
PROMPT_EOF

cat > .claude/loops/02-sonnet-todo.txt << PROMPT_EOF
/loop 5m You are a Sonnet TODO Worker loop for ${GITHUB_USER}/${GITHUB_REPO}. You handle SIMPLE tasks only. On each iteration: 1) Check GitHub Issues labeled \`claude-sonnet\` using mcp__github__list_issues (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, labels: ["claude-sonnet"]). 2) ALSO check issues labeled \`claude-wip\` that were originally \`claude-sonnet\` for new user comments — use mcp__github__issue_read with method "get_comments" to see if the user has replied with decisions, approvals, or feedback. 3) Scan for inline \`// TODO(@claude):\` comments in ${SRC_DIR:-src}/ — these default to Sonnet unless the comment indicates complex work. 4) If nothing found and no new user comments on wip issues, stop. 5) Pick ONE item (prefer issues over TODOs). 6) For issues: relabel claude-sonnet→claude-wip via mcp__github__issue_write. 7) Check if the issue is a sub-task of a larger epic. If yes: create worktree from the epic branch (claude/epic-<name>), PR against that branch. If no: create worktree from origin/main, PR against main. 8) Implement the change, run ${VERIFY_CHAIN:-tests}, commit+push. 9) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: branch, base: epic-branch-or-main) with title from the issue and body that says "Closes #N" plus a summary. 10) Comment on the issue with the PR link via mcp__github__add_issue_comment. 11) Clean up worktree. If stuck after 3 attempts: add \`claude-blocked\` label and add comment "Blocked — may need opus-level reasoning. Consider relabeling to claude-opus." 12) For inline TODOs: same flow — implement, remove the comment, commit+push, create PR. 13) For wip issues with new user comments: incorporate feedback, reply acknowledging. 14) IMPORTANT: Always use available MCP tools directly. One item per iteration. Never force push.
PROMPT_EOF

cat > .claude/loops/03-lint-guardian.txt << PROMPT_EOF
/loop 30m You are a Lint Guardian loop for ${GITHUB_USER}/${GITHUB_REPO}. On each iteration: 1) FIRST check if there's already an open PR with "lint" in the title that YOU created (author is the bot) via mcp__github__list_pull_requests (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, state: open). If an unmerged lint-fix PR already exists, skip this iteration — wait for it to be merged. 2) Create worktree from origin/main. 3) ${INSTALL_CMD:-npm ci} && ${LINT_CMD:+${LINT_CMD} --fix ||} npx eslint ${SRC_DIR:-src}/ --fix. 4) If no file changes (git diff --stat is empty), clean up and stop — lint is clean. 5) If changes: commit "fix: auto-fix lint violations", verify ${VERIFY_CHAIN:-lint+build+test pass}, push branch. 6) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: branch, base: main) with title "fix: auto-fix lint violations" and body summarizing what was fixed. 7) Clean up worktree. Never force push.
PROMPT_EOF

cat > .claude/loops/04-build-watchdog.txt << PROMPT_EOF
/loop 30m You are a Build Watchdog loop for ${GITHUB_USER}/${GITHUB_REPO}. On each iteration: 1) FIRST check if there's already an open PR with "build" or "type error" in the title that YOU created via mcp__github__list_pull_requests (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, state: open). If an unmerged build-fix PR already exists, skip this iteration — wait for it to be merged. 2) Create worktree from origin/main. 3) ${INSTALL_CMD:-npm ci}. 4) Run ${BUILD_CMD:-npm run build}. 5) If build succeeds with zero errors, clean up and stop — build is green. 6) If type errors or compilation failures: fix them, verify ${VERIFY_CHAIN:-lint+build+test}, commit+push. 7) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: branch, base: main) with title "fix: resolve build errors" and body describing what was fixed. 8) Clean up worktree. If stuck after 3 attempts, stop and report. Never force push.
PROMPT_EOF

cat > .claude/loops/05-pr-responder.txt << PROMPT_EOF
/loop 5m You are a PR Comment Responder loop for ${GITHUB_USER}/${GITHUB_REPO}. On each iteration: 1) List open PRs via mcp__github__list_pull_requests (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}). 2) Check BOTH inline review comments (get_review_comments) AND general PR comments (get_comments) via mcp__github__pull_request_read. 3) Filter for actionable requests — skip approvals, acks, bot messages. If none, stop. 4) Assess each comment's complexity: if the requested change is a simple fix (typo, rename, small tweak, adding a check), handle it. If the comment requests a complex rework (redesign component, rethink approach, add substantial new logic), reply acknowledging and add a comment: "This requires deeper changes — escalating to opus session" and add label \`claude-opus\` to the PR. 5) For changes you handle (max 3): create worktree from PR branch, make the requested change, verify ${VERIFY_CHAIN:-lint+build+test}, commit+push to the PR branch. 6) For inline comments: reply via mcp__github__add_reply_to_pull_request_comment. For general comments: reply via mcp__github__add_issue_comment. Body: "Addressed in latest commit." 7) If the comment asks you to run something (SQL, deploy, etc.) and you have an MCP tool for it, execute it directly. 8) Clean up worktree. Never force push.
PROMPT_EOF

cat > .claude/loops/06-opus-todo.txt << PROMPT_EOF
/loop 5m You are an Opus TODO Worker loop for ${GITHUB_USER}/${GITHUB_REPO}. You handle COMPLEX tasks that require deep reasoning. On each iteration: 1) Check GitHub Issues labeled \`claude-opus\` using mcp__github__list_issues (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, labels: ["claude-opus"]). 2) ALSO check issues labeled \`claude-wip\` that were originally \`claude-opus\` for new user comments — use mcp__github__issue_read with method "get_comments". 3) ALSO check open PRs labeled \`claude-opus\` (escalated from Sonnet PR Comment Responder) via mcp__github__list_pull_requests — these are PRs needing complex rework based on review feedback. 4) If nothing found, stop. 5) Pick ONE item (prefer escalated PRs > issues > inline TODOs). 6) For issues: relabel claude-opus→claude-wip via mcp__github__issue_write. 7) Check if the issue is an epic (describes a large project with sub-tasks). If yes: create the epic branch \`claude/epic-<name>\`, break down into sub-issues (label each \`claude-opus\` or \`claude-sonnet\` based on sub-task complexity), and create them via mcp__github__issue_write. 8) For regular issues: check if it's a sub-task of a larger epic. If yes: create worktree from the epic branch, PR against that branch. If no: create worktree from origin/main, PR against main. 9) Implement the change, run ${VERIFY_CHAIN:-tests}, commit+push. 10) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: branch, base: epic-branch-or-main) with title from the issue and body that says "Closes #N" plus a thorough summary of the approach and changes. 11) Comment on the issue with the PR link via mcp__github__add_issue_comment. 12) For escalated PRs: create worktree from the PR branch, address the complex review feedback, verify, commit+push, reply to the comments. 13) Clean up worktree. If stuck after 3 attempts: add \`claude-blocked\` label. 14) IMPORTANT: Always use available MCP tools directly. One item per iteration. Never force push.
PROMPT_EOF

cat > .claude/loops/07-quality-sweep.txt << PROMPT_EOF
/loop 1h You are a Code Quality Sweep loop for ${GITHUB_USER}/${GITHUB_REPO}. On each iteration: 1) FIRST check if there's already an open PR with "refactor:" or "quality" in the title via mcp__github__list_pull_requests (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, state: open). If 3+ quality PRs are open, skip this iteration — let the human merge before creating more. 2) Create worktree from origin/main. 3) Search for: TODO/FIXME/HACK comments (not @claude), unused exports, functions >80 lines, duplicated code blocks. 4) Fix exactly ONE issue. 5) Verify ${VERIFY_CHAIN:-lint+build+test}. 6) Commit+push. 7) Create a PR via mcp__github__create_pull_request (owner: ${GITHUB_USER}, repo: ${GITHUB_REPO}, head: branch, base: main) with title "refactor: <summary>" and body describing the quality issue fixed. 8) Clean up worktree. 9) If nothing found, stop — codebase is clean. One issue per iteration. Max 2 files per commit. Never force push.
PROMPT_EOF

echo "✅ Created .claude/loops/ (7 prompt files)"

# --- Generate start-loops.sh ---
cat > start-loops.sh << 'STARTLOOPS_EOF'
#!/usr/bin/env zsh
#
# Start the Claude agent fleet — two tmux sessions, 7 loops total.
# Usage: ./start-loops.sh          (start both sessions)
#        ./start-loops.sh sonnet   (start only Sonnet session)
#        ./start-loops.sh opus     (start only Opus session)
#        ./start-loops.sh stop     (kill both sessions)
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

SONNET_SESSION="claude-sonnet"
OPUS_SESSION="claude-opus"
LOOP_DIR=".claude/loops"

# --- Preflight checks ---
if ! command -v tmux &>/dev/null; then
  echo "❌ tmux is required. Install: sudo apt install tmux (or brew install tmux)"
  exit 1
fi

if ! command -v claude &>/dev/null; then
  echo "❌ claude CLI not found in PATH"
  exit 1
fi

if [[ ! -d "$LOOP_DIR" ]]; then
  echo "❌ $LOOP_DIR not found. Run setup.sh first."
  exit 1
fi

# --- Helper: send a prompt file to a tmux session ---
send_loop() {
  local session="$1"
  local file="$2"
  local name=$(basename "$file" .txt)

  # Load file into tmux paste buffer and send it
  tmux load-buffer -b loop-cmd "$file"
  tmux paste-buffer -b loop-cmd -t "$session"
  tmux send-keys -t "$session" Enter
  echo "   ✓ $name"

  # Wait for Claude to accept the /loop command before sending next
  sleep 3
}

# --- Stop command ---
if [[ "${1:-}" == "stop" ]]; then
  echo "Stopping agent sessions..."
  tmux kill-session -t "$SONNET_SESSION" 2>/dev/null && echo "  ✓ Killed $SONNET_SESSION" || echo "  - $SONNET_SESSION not running"
  tmux kill-session -t "$OPUS_SESSION" 2>/dev/null && echo "  ✓ Killed $OPUS_SESSION" || echo "  - $OPUS_SESSION not running"
  exit 0
fi

# --- Start Sonnet session ---
start_sonnet() {
  if tmux has-session -t "$SONNET_SESSION" 2>/dev/null; then
    echo "⚠️  Session '$SONNET_SESSION' already running. Kill it first: ./start-loops.sh stop"
    return 1
  fi

  echo "🚀 Starting Sonnet session ($SONNET_SESSION)..."
  tmux new-session -d -s "$SONNET_SESSION" "claude --model claude-sonnet-4-6"

  # Wait for Claude to initialize
  echo "   Waiting for Claude to start..."
  sleep 8

  # Send the 5 Sonnet loops
  for f in "$LOOP_DIR"/0{1,2,3,4,5}-*.txt; do
    [[ -f "$f" ]] && send_loop "$SONNET_SESSION" "$f"
  done

  echo "✅ Sonnet session running with 5 loops"
  echo "   Attach: tmux attach -t $SONNET_SESSION"
}

# --- Start Opus session ---
start_opus() {
  if tmux has-session -t "$OPUS_SESSION" 2>/dev/null; then
    echo "⚠️  Session '$OPUS_SESSION' already running. Kill it first: ./start-loops.sh stop"
    return 1
  fi

  echo "🚀 Starting Opus session ($OPUS_SESSION)..."
  tmux new-session -d -s "$OPUS_SESSION" "claude --model claude-opus-4-6"

  # Wait for Claude to initialize
  echo "   Waiting for Claude to start..."
  sleep 8

  # Send the 2 Opus loops
  for f in "$LOOP_DIR"/0{6,7}-*.txt; do
    [[ -f "$f" ]] && send_loop "$OPUS_SESSION" "$f"
  done

  echo "✅ Opus session running with 2 loops"
  echo "   Attach: tmux attach -t $OPUS_SESSION"
}

# --- Main ---
case "${1:-all}" in
  sonnet) start_sonnet ;;
  opus)   start_opus ;;
  all)
    start_sonnet
    echo ""
    start_opus
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Agent fleet running: 7 loops across 2 sessions"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  Sonnet (5 loops): tmux attach -t $SONNET_SESSION"
    echo "  Opus   (2 loops): tmux attach -t $OPUS_SESSION"
    echo "  Stop all:         ./start-loops.sh stop"
    echo "  List sessions:    tmux ls"
    echo ""
    ;;
  *)
    echo "Usage: ./start-loops.sh [sonnet|opus|stop|all]"
    exit 1
    ;;
esac
STARTLOOPS_EOF
chmod +x start-loops.sh
echo "✅ Created start-loops.sh"

# --- Summary ---
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Bootstrap complete for $REPO_NAME"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Next steps:"
echo "  1. Create GitHub labels: claude-ready, claude-sonnet, claude-opus, claude-wip, claude-blocked"
echo "  2. Commit .claude/settings.json, CLAUDE.md, .github/workflows/ci.yml"
echo "  3. Run one-shot tasks from LOOPS.md (Test Bootstrapper, Type Strictifier)"
echo "  4. Start the agent fleet:"
echo ""
echo "     ./start-loops.sh          # start both sessions (7 loops)"
echo "     ./start-loops.sh sonnet   # start only Sonnet (5 loops)"
echo "     ./start-loops.sh opus     # start only Opus (2 loops)"
echo "     ./start-loops.sh stop     # kill both sessions"
echo ""
echo "  5. File issues with claude-ready label (auto-triaged) or claude-sonnet/claude-opus directly"
echo "  6. Monitor: tmux attach -t claude-sonnet  /  tmux attach -t claude-opus"
echo "  7. After 3 days: see 'After the Initial 3-Day Period' in LOOPS.md"
echo ""
