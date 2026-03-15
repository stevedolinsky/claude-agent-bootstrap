# Claude Agent Bootstrap

Turn any repo into an autonomous AI development environment. One script sets up
Claude Code agent loops that pick up GitHub issues, fix lint/build errors,
respond to PR review comments, and refactor code — all without human intervention.

## What You Get

- **Agent fleet**: 5-7 loops running in tmux that process issues, fix builds, respond to reviews
- **Works with**: Node.js, Python, Go, Rust, or any project
- **Model routing**: use Sonnet for simple tasks, Opus for complex ones (or single-model mode)
- **Zero ongoing maintenance**: file issues, review PRs, merge — agents handle the rest

## Prerequisites

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed (`claude` command available)
- GitHub MCP server configured:
  ```bash
  claude mcp add github -- npx -y @modelcontextprotocol/server-github
  export GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...
  ```
- tmux (optional but recommended — enables `./start-loops.sh` automation)

## Quick Start

1. Clone this repo:
   ```bash
   git clone https://github.com/stevedolinsky/claude-agent-bootstrap.git
   ```

2. Run setup.sh from **your project's root**:
   ```bash
   cd your-project
   ~/claude-agent-bootstrap/setup.sh
   ```

3. Answer 3 questions (model mode, labels, loop speed), then:
   ```bash
   git push           # activates GHA workflows
   ./start-loops.sh   # starts Claude sessions (+ webhook receiver if Tailscale)
   ```

That's it. The agents are running.

### Non-interactive mode

```bash
~/claude-agent-bootstrap/setup.sh --defaults
```
Uses: single-sonnet, auto-labels, normal speed. Good for CI or scripting.

## Event-Driven Architecture

Instead of Claude polling GitHub every few minutes (expensive, noisy), setup.sh generates
GitHub Actions workflows that push events to Claude the instant something happens.

**Mode is auto-detected at setup time** — no choice required:

| Condition | Mode | How it works |
|-----------|------|-------------|
| Tailscale running on your machine | **Tailscale Push** | GHA joins your Tailscale network, POSTs event to your machine. Claude only runs when there's actual work. |
| No Tailscale | **Queue Branch** | GHA writes JSON to a `claude-queue` branch. Claude reads the queue each loop iteration. |

### Tailscale Mode (zero polling)

```
Issue labeled →  GHA workflow  →  joins Tailscale  →  POST /webhook  →  webhook-receiver.py
                                                                              │
                                                                    tmux new-window
                                                                    claude --print "process issue #N"
                                                                    (fresh session, exits when done)
```

**One-time setup** (if you haven't already):

1. **Create the Tailscale tag** — tailscale.com → Access Controls → add to `tagOwners`:
   ```json
   "tagOwners": { "tag:ci": [] }
   ```

2. **Create OAuth client** — tailscale.com → Settings → OAuth clients:
   - Scope: `devices:core:write`
   - Tag: `tag:ci`

3. **Add 3 secrets** to GitHub repo Settings → Secrets → Actions:
   | Secret | Value |
   |--------|-------|
   | `TS_OAUTH_CLIENT_ID` | From OAuth client above |
   | `TS_OAUTH_SECRET` | From OAuth client above |
   | `AGENT_WEBHOOK_SECRET` | Printed by setup.sh at end of run |

The machine hostname is baked into the workflow at setup time. The `webhook-receiver.py`
runs locally (started by `./start-loops.sh`). Each issue/PR comment spawns a fresh Claude
session — zero context accumulation over time.

**Generated files (Tailscale mode):**
- `.github/workflows/agent-webhook-issue.yml` — fires on `issues: labeled`
- `.github/workflows/agent-webhook-pr-comment.yml` — fires on PR/issue comments
- `webhook-receiver.py` — local server (gitignored, started by start-loops.sh)

### Queue Branch Mode (no secrets needed)

```
Issue labeled →  GHA workflow  →  writes JSON  →  claude-queue/pending/<item>.json
                                                              │
                                                   Claude reads queue each loop iteration
                                                   (1 API call vs ~50K tokens of scanning)
```

No secrets required — uses the built-in `GITHUB_TOKEN`.

**Generated files (queue mode):**
- `.github/workflows/agent-queue-issue.yml` — writes issue events to queue branch
- `.github/workflows/agent-queue-pr-comment.yml` — writes PR comment events to queue branch
- `claude-queue` branch on GitHub — orphan branch with `pending/` and `processed/` dirs

### Changing the Tailscale tag

The default tag is `tag:ci`. To use a different tag (e.g. `tag:github-actions`):
1. Delete `.claude/bootstrap.conf` in your project
2. Re-run `setup.sh` — it picks up the new default
   OR edit the generated workflow files directly and change `tag:ci` to your tag

## How It Works

```
Issue filed → [Triage Worker] → Sonnet or Opus Worker → PR created → You review → Merge
```

### Model Modes

| Mode | Sessions | Loops | Best for |
|------|----------|-------|----------|
| Dual (Sonnet+Opus) | 2 | 7 | Heavy workloads, mixed complexity |
| Single (Sonnet) | 1 | 5 | Cost-effective, smaller projects |
| Single (Opus) | 1 | 5 | Maximum quality, complex codebases |

### The Loops

| Loop | What it does | When |
|------|-------------|------|
| Triage Worker | Classifies new issues by complexity | Dual mode only |
| TODO Worker | Picks up issues, implements, creates PRs | Always |
| Lint Guardian | Auto-fixes lint violations | If lint configured |
| Build Watchdog | Fixes build/type errors | If build configured |
| PR Comment Responder | Addresses review feedback | Always |
| Code Quality Sweep | Refactors one issue per cycle | Always |

### Filing Work

- **GitHub Issues**: add `claude-ready` label (or `claude-sonnet`/`claude-opus` to skip triage in dual mode)
- **Inline**: add `// TODO(@claude): description` in source code
- **Epics**: file a "Plan: ..." issue — agent breaks into sub-tasks automatically

## Generated Files

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Agent instructions (appended to existing) |
| `LOOPS.md` | Loop documentation and manual paste commands |
| `.claude/settings.json` | Permissions for autonomous operation |
| `.claude/loops/*.txt` | Individual loop prompts |
| `.claude/bootstrap.conf` | Your config (model mode, speed, Tailscale tag) |
| `start-loops.sh` | tmux launcher (also starts webhook receiver) |
| `.github/workflows/ci.yml` | CI pipeline (if applicable) |
| `.github/workflows/agent-webhook-*.yml` | GHA → local push (Tailscale mode) |
| `.github/workflows/agent-queue-*.yml` | GHA → queue branch (queue mode) |
| `webhook-receiver.py` | Local webhook server, gitignored (Tailscale mode) |

## Managing the Fleet

```bash
./start-loops.sh          # start all sessions
./start-loops.sh stop     # kill all sessions
./start-loops.sh --dry-run # print prompts without starting
```

Dual mode also supports:
```bash
./start-loops.sh sonnet   # start only Sonnet session
./start-loops.sh opus     # start only Opus session
tmux attach -t claude-sonnet  # watch Sonnet session
tmux attach -t claude-opus    # watch Opus session
```

## After 3 Days

Claude Code sessions expire after ~3 days. To restart:
```bash
./start-loops.sh stop && ./start-loops.sh
```

See `LOOPS.md` in your project for detailed renewal instructions.

## Supported Languages

| Language | Detection | CI | Lint | Build | Test |
|----------|-----------|-----|------|-------|------|
| Node.js | `package.json` | Yes | eslint | tsc/next/vite | vitest/jest |
| Python | `pyproject.toml` / `requirements.txt` | Yes | ruff | — | pytest |
| Go | `go.mod` | Yes | golangci-lint / go vet | go build | go test |
| Rust | `Cargo.toml` | Yes | clippy | cargo build | cargo test |
| Generic | (fallback) | No | manual | manual | manual |

## Re-running Setup

Running `setup.sh` again in a project that was already bootstrapped will:
- Source saved config from `.claude/bootstrap.conf` (skip prompts)
- Regenerate loop files, start-loops.sh, LOOPS.md
- Skip CLAUDE.md if agent instructions already present
- Skip CI if workflow already exists

To change config, delete `.claude/bootstrap.conf` and re-run.

## License

MIT
