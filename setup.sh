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

    # GHA workflows
    mkdir -p .github/workflows
    for wf in "${TEMPLATES_DIR}"/workflows/*.yml; do
        cp "$wf" ".github/workflows/$(basename "$wf")"
        ok "  .github/workflows/$(basename "$wf")"
    done

    # .claude/settings.json
    mkdir -p .claude
    if [[ ! -f ".claude/settings.json" ]]; then
        cp "${TEMPLATES_DIR}/settings.json" ".claude/settings.json"
        ok "  .claude/settings.json"
    else
        warn "  .claude/settings.json exists, skipping"
    fi

    # CLAUDE.md
    local events_file="${HOME}/.claude/agent-events.jsonl"
    local plans_dir="${HOME}/.claude/plans"

    if [[ -f "CLAUDE.md" ]] && grep -q "Agent Fleet Configuration" CLAUDE.md 2>/dev/null; then
        warn "  CLAUDE.md already configured, skipping"
    else
        local content
        content=$(sed \
            -e "s|{{verify_chain}}|${VERIFY_CHAIN}|g" \
            -e "s|{{events_file}}|${events_file}|g" \
            -e "s|{{plans_dir}}|${plans_dir}|g" \
            "${TEMPLATES_DIR}/claude-md-append.md")

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
        info "Webhook secret exists at ${secret_file}"
    else
        mkdir -p "${HOME}/.claude"
        openssl rand -hex 32 > "$secret_file"
        chmod 600 "$secret_file"
        ok "Generated webhook secret"
        echo ""
        warn "Add these GitHub repo secrets:"
        warn "  AGENT_WEBHOOK_SECRET = $(cat "$secret_file")"
        [[ "$HAS_TAILSCALE" == "true" ]] && warn "  AGENT_RECEIVER_HOST = ${TS_IP}"
    fi
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
    info "Next steps:"
    echo "  1. Add GitHub secrets (AGENT_WEBHOOK_SECRET, TS_OAUTH_CLIENT_ID, TS_OAUTH_SECRET, AGENT_RECEIVER_HOST)"
    echo "  2. Start receiver: python -m receiver"
    echo "  3. Label an issue with 'agent'"
    echo ""
}

main "$@"
