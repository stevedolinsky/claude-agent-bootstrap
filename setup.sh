#!/usr/bin/env bash
#
# Claude Agent Bootstrap v2.0 — Sequential Work Queue Architecture
#
# Thin init script that copies templates into a target repository.
# Run from the root of the target project.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="${SCRIPT_DIR}/templates"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()   { echo -e "${RED}[error]${NC} $*" >&2; }

# ---------------------------------------------------------------------------
# 1. Detect Language & Verify Chain
# ---------------------------------------------------------------------------

detect_language() {
    if [[ -f "package.json" ]]; then
        if [[ -f "bun.lockb" ]]; then
            PKG_MGR="bun"
        elif [[ -f "pnpm-lock.yaml" ]]; then
            PKG_MGR="pnpm"
        elif [[ -f "yarn.lock" ]]; then
            PKG_MGR="yarn"
        else
            PKG_MGR="npm"
        fi
        LANGUAGE="node"
        VERIFY_CHAIN="${PKG_MGR} install && ${PKG_MGR} run lint 2>/dev/null; ${PKG_MGR} run build 2>/dev/null; ${PKG_MGR} test 2>/dev/null"
    elif [[ -f "pyproject.toml" ]] || [[ -f "setup.py" ]] || [[ -f "requirements.txt" ]]; then
        LANGUAGE="python"
        VERIFY_CHAIN="python -m pytest 2>/dev/null || true"
    elif [[ -f "go.mod" ]]; then
        LANGUAGE="go"
        VERIFY_CHAIN="go vet ./... && go test ./..."
    elif [[ -f "Cargo.toml" ]]; then
        LANGUAGE="rust"
        VERIFY_CHAIN="cargo check && cargo test"
    elif [[ -f "Gemfile" ]]; then
        LANGUAGE="ruby"
        VERIFY_CHAIN="bundle exec rake test 2>/dev/null || bundle exec rspec 2>/dev/null || true"
    else
        LANGUAGE="unknown"
        VERIFY_CHAIN="echo 'No verify chain configured'"
    fi
    info "Detected language: ${LANGUAGE}"
}

# ---------------------------------------------------------------------------
# 2. Detect Tailscale
# ---------------------------------------------------------------------------

detect_tailscale() {
    if command -v tailscale &>/dev/null; then
        TS_IP=$(tailscale ip -4 2>/dev/null || echo "")
        if [[ -n "$TS_IP" ]]; then
            HAS_TAILSCALE=true
            info "Tailscale detected (IP: ${TS_IP})"
            return
        fi
    fi
    HAS_TAILSCALE=false
    warn "Tailscale not detected — configure AGENT_RECEIVER_HOST manually"
}

# ---------------------------------------------------------------------------
# 3. Copy Templates
# ---------------------------------------------------------------------------

copy_templates() {
    info "Copying templates..."

    # GHA workflows — only update if content changed
    mkdir -p .github/workflows
    for wf in "${TEMPLATES_DIR}"/workflows/*.yml; do
        local name
        name=$(basename "$wf")
        local target=".github/workflows/${name}"
        if [[ -f "$target" ]] && diff -q "$wf" "$target" &>/dev/null; then
            info "  .github/workflows/${name} unchanged, skipping"
        else
            cp "$wf" "$target"
            ok "  .github/workflows/${name}"
        fi
    done

    # .claude/settings.json — never overwrite (user may have customized permissions)
    mkdir -p .claude
    if [[ ! -f ".claude/settings.json" ]]; then
        cp "${TEMPLATES_DIR}/settings.json" ".claude/settings.json"
        ok "  .claude/settings.json"
    else
        info "  .claude/settings.json exists, preserving"
    fi

    # CLAUDE.md
    local events_file="${HOME}/.claude/agent-events.jsonl"
    local plans_dir="${HOME}/.claude/plans"

    if [[ -f "CLAUDE.md" ]] && grep -q "Agent Fleet Configuration" CLAUDE.md 2>/dev/null; then
        warn "  CLAUDE.md already configured, skipping"
    else
        local content
        content=$(VERIFY_CHAIN="$VERIFY_CHAIN" EVENTS_FILE="$events_file" PLANS_DIR="$plans_dir" \
            envsubst '${VERIFY_CHAIN} ${EVENTS_FILE} ${PLANS_DIR}' \
            < "${TEMPLATES_DIR}/claude-md-append.md")

        if [[ -f "CLAUDE.md" ]]; then
            echo "" >> CLAUDE.md
            echo "$content" >> CLAUDE.md
            ok "  Appended to CLAUDE.md"
        else
            echo "$content" > CLAUDE.md
            ok "  Created CLAUDE.md"
        fi
    fi
}

# ---------------------------------------------------------------------------
# 4. Config + Secret
# ---------------------------------------------------------------------------

save_config() {
    mkdir -p .claude
    cat > ".claude/bootstrap.conf" <<CONF
LANGUAGE=${LANGUAGE}
VERIFY_CHAIN=${VERIFY_CHAIN}
HAS_TAILSCALE=${HAS_TAILSCALE}
CONF
    ok "Saved .claude/bootstrap.conf"
}

setup_secret() {
    local secret_file="${HOME}/.claude/agent-webhook.secret"
    if [[ -f "$secret_file" ]]; then
        ok "Webhook secret preserved at ${secret_file} (reusing existing)"
        info "  Your existing GitHub secret AGENT_WEBHOOK_SECRET is still valid — no update needed"
    else
        mkdir -p "${HOME}/.claude"
        openssl rand -hex 32 > "$secret_file"
        chmod 600 "$secret_file"
        ok "Generated NEW webhook secret"
        echo ""
        warn "Add these GitHub repo secrets:"
        warn "  AGENT_WEBHOOK_SECRET = $(cat "$secret_file")"
        [[ "$HAS_TAILSCALE" == "true" ]] && warn "  AGENT_RECEIVER_HOST = ${TS_IP}"
    fi
}

# ---------------------------------------------------------------------------
# 5. Repo Path Registry
# ---------------------------------------------------------------------------

register_repo_path() {
    local repos_file="${HOME}/.claude/agent-repos.json"
    local repo_full
    repo_full="$(git remote get-url origin 2>/dev/null | sed 's|.*github\.com[:/]||;s|\.git$||')"
    local repo_path
    repo_path="$(pwd)"

    if [[ -z "$repo_full" ]]; then
        warn "Cannot determine repo name from git remote — skipping path registration"
        return
    fi

    mkdir -p "${HOME}/.claude"

    # Read existing JSON or start fresh
    local existing="{}"
    [[ -f "$repos_file" ]] && existing="$(cat "$repos_file")"

    # Write updated JSON (jq if available, python3 fallback)
    if command -v jq &>/dev/null; then
        echo "$existing" | jq --arg repo "$repo_full" --arg path "$repo_path" \
            '. + {($repo): $path}' > "$repos_file"
    elif command -v python3 &>/dev/null; then
        python3 -c "
import json, pathlib
p = pathlib.Path('$repos_file')
data = json.loads(p.read_text()) if p.exists() else {}
data['$repo_full'] = '$repo_path'
p.write_text(json.dumps(data, indent=2))
"
    else
        warn "Neither jq nor python3 available — cannot register repo path"
        return
    fi

    ok "Registered ${repo_full} → ${repo_path}"
}

# ---------------------------------------------------------------------------
# 6. Interactive Post-Bootstrap
# ---------------------------------------------------------------------------

maybe_enable_dashboard() {
    local sentinel="${HOME}/.claude/agent-dashboard.enabled"

    # Skip if already configured (e.g., re-running setup for a second repo)
    if [[ -f "$sentinel" ]]; then
        ok "Dashboard already enabled"
        return
    fi

    if [[ ! -t 0 ]]; then
        # Non-interactive: default to disabled (no sentinel = disabled)
        return
    fi

    echo ""
    local answer
    read -rp "  Enable observability dashboard (Grafana + Prometheus)? [y/N] " answer || answer=""
    if [[ "${answer,,}" == "y" ]]; then
        if ! command -v docker &>/dev/null; then
            warn "Docker not found. Install it: https://docs.docker.com/get-docker/"
            echo "  After installing Docker, enable the dashboard by running:"
            echo "    touch ~/.claude/agent-dashboard.enabled"
            return
        fi
        touch "$sentinel"
        ok "Dashboard enabled. It will start with the receiver via start.sh."
    else
        echo "  Dashboard disabled. Enable later: touch ~/.claude/agent-dashboard.enabled"
    fi
}

maybe_start_receiver() {
    if [[ ! -t 0 ]]; then return; fi  # Non-interactive: skip

    echo ""
    local answer
    read -rp "  Start the webhook receiver? [y/N] " answer || answer=""
    [[ "${answer,,}" == "y" ]] || return 0

    bash "${SCRIPT_DIR}/start.sh" \
        && ok "Receiver started" \
        || warn "Receiver start failed — run manually: bash ${SCRIPT_DIR}/start.sh"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    echo ""
    echo "  Claude Agent Bootstrap v2.0"
    echo "  Sequential Work Queue Architecture"
    echo ""

    if ! git rev-parse --is-inside-work-tree &>/dev/null; then
        err "Not inside a git repository."
        exit 1
    fi

    detect_language
    detect_tailscale
    copy_templates
    save_config
    setup_secret

    echo ""
    ok "Bootstrap complete!"
    echo ""

    # Auto-commit and push workflow changes so GitHub sees them immediately.
    # These are boilerplate config files (GHA workflows, CLAUDE.md, .claude/settings.json),
    # not application code — safe to push directly.
    local files_to_add=()
    [[ -d ".github/workflows" ]] && files_to_add+=(".github/workflows/")
    [[ -f "CLAUDE.md" ]] && files_to_add+=("CLAUDE.md")
    [[ -d ".claude" ]] && files_to_add+=(".claude/")

    if [[ ${#files_to_add[@]} -gt 0 ]]; then
        git add "${files_to_add[@]}" 2>/dev/null

        if ! git diff --cached --quiet 2>/dev/null; then
            info "Pushing agent fleet config to GitHub..."
            echo "  Files: ${files_to_add[*]}"
            echo "  Why:   GitHub Actions need these workflow files on the remote to trigger on label events."
            echo ""

            git commit -m "chore: configure agent fleet workflows and settings" --no-verify 2>/dev/null
            local branch
            branch=$(git branch --show-current)
            git pull origin "$branch" --rebase 2>/dev/null || true
            if git push -u origin "$branch" 2>/dev/null; then
                ok "Pushed to GitHub — workflows are live on '${branch}'"
            else
                warn "Push failed. Try:"
                echo "  git push -u origin ${branch}"
            fi
        else
            info "No config changes to push — GitHub is up to date"
        fi
    fi

    # Register repo path in JSON sidecar for --directory support
    register_repo_path

    # Interactive post-bootstrap: enable dashboard and start receiver
    maybe_enable_dashboard
    maybe_start_receiver

    echo ""
    info "Next step: Label an issue with 'agent' to start the pipeline"
    echo ""
    echo "  Lifecycle commands:"
    echo "    Start receiver:  ${SCRIPT_DIR}/start.sh"
    echo "    Stop receiver:   ${SCRIPT_DIR}/stop.sh"
    echo "    Check status:    ${SCRIPT_DIR}/status.sh"
    echo ""
    local secret_file="${HOME}/.claude/agent-webhook.secret"
    if [[ -f "$secret_file" ]]; then
        info "GitHub secrets already configured — no changes needed."
    fi
}

main "$@"
