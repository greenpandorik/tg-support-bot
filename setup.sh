#!/usr/bin/env bash
# =============================================================================
# setup.sh — one-command setup & deploy for tg-support-bot
#
# Usage:
#   ./setup.sh           — first-time install or redeploy (rebuild + restart)
#   ./setup.sh --rebuild — force image rebuild
#   ./setup.sh --stop    — stop all containers
#   ./setup.sh --logs    — follow container logs
#
# Supported OS: macOS, Debian/Ubuntu, Fedora/RHEL/CentOS, Arch Linux
# =============================================================================

set -euo pipefail

COMPOSE_FILE="$(cd "$(dirname "$0")" && pwd)/docker-compose.yml"
ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.env"
ENV_EXAMPLE="$(cd "$(dirname "$0")" && pwd)/.env.example"
DATA_DIR="$(cd "$(dirname "$0")" && pwd)/.data"
REDIS_DATA_DIR="$(cd "$(dirname "$0")" && pwd)/redis/data"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { echo "[setup] $*"; }
warn() { echo "[setup] WARNING: $*" >&2; }
fail() { echo "[setup] ERROR: $*" >&2; exit 1; }

detect_os() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux)
            if   [ -f /etc/debian_version ];  then echo "debian"
            elif [ -f /etc/fedora-release ];   then echo "fedora"
            elif [ -f /etc/redhat-release ];   then echo "rhel"
            elif [ -f /etc/arch-release ];     then echo "arch"
            else echo "linux"
            fi ;;
        *) echo "unknown" ;;
    esac
}

# ---------------------------------------------------------------------------
# Docker installation
# ---------------------------------------------------------------------------

install_docker_macos() {
    if command -v docker &>/dev/null; then return; fi
    log "Docker not found. Please install Docker Desktop for Mac from https://docs.docker.com/desktop/install/mac-install/"
    fail "Install Docker Desktop, then re-run this script."
}

install_docker_debian() {
    if command -v docker &>/dev/null; then return; fi
    log "Installing Docker on Debian/Ubuntu..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq ca-certificates curl gnupg lsb-release
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
        sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    sudo systemctl enable --now docker
    log "Docker installed. You may need to log out and back in for group changes to take effect."
}

install_docker_fedora() {
    if command -v docker &>/dev/null; then return; fi
    log "Installing Docker on Fedora..."
    sudo dnf -y install dnf-plugins-core
    sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
    sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
    sudo systemctl enable --now docker
}

install_docker_rhel() {
    if command -v docker &>/dev/null; then return; fi
    log "Installing Docker on RHEL/CentOS..."
    sudo yum install -y yum-utils
    sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    sudo yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    sudo systemctl enable --now docker
}

install_docker_arch() {
    if command -v docker &>/dev/null; then return; fi
    log "Installing Docker on Arch Linux..."
    sudo pacman -Sy --noconfirm docker docker-compose
    sudo systemctl enable --now docker
}

ensure_docker() {
    OS=$(detect_os)
    case "$OS" in
        macos)  install_docker_macos ;;
        debian) install_docker_debian ;;
        fedora) install_docker_fedora ;;
        rhel)   install_docker_rhel ;;
        arch)   install_docker_arch ;;
        *)
            command -v docker &>/dev/null || fail "Docker not found. Please install Docker manually."
            ;;
    esac

    docker info &>/dev/null || fail "Docker daemon is not running. Start it and re-run this script."
}

# ---------------------------------------------------------------------------
# docker compose wrapper (handles both v1 and v2)
# ---------------------------------------------------------------------------

compose() {
    if docker compose version &>/dev/null 2>&1; then
        docker compose -f "$COMPOSE_FILE" "$@"
    elif command -v docker-compose &>/dev/null; then
        docker-compose -f "$COMPOSE_FILE" "$@"
    else
        fail "Neither 'docker compose' (v2) nor 'docker-compose' (v1) found."
    fi
}

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

cmd_stop() {
    log "Stopping containers..."
    compose down
    log "Done."
}

cmd_logs() {
    compose logs -f --tail=100
}

cmd_deploy() {
    local rebuild="${1:-}"

    ensure_docker

    # Prepare .env
    if [ ! -f "$ENV_FILE" ]; then
        if [ -f "$ENV_EXAMPLE" ]; then
            cp "$ENV_EXAMPLE" "$ENV_FILE"
            warn ".env not found — copied from .env.example."
            warn "Open .env, fill in BOT_TOKEN, BOT_DEV_ID, BOT_GROUP_ID, and SUPPORT_ADMIN_IDS, then re-run this script."
            exit 1
        else
            fail ".env not found and no .env.example to copy from."
        fi
    fi

    # Validate required variables
    source "$ENV_FILE" 2>/dev/null || true
    local missing=()
    [ -z "${BOT_TOKEN:-}" ]    && missing+=("BOT_TOKEN")
    [ -z "${BOT_DEV_ID:-}" ]   && missing+=("BOT_DEV_ID")
    [ -z "${BOT_GROUP_ID:-}" ] && missing+=("BOT_GROUP_ID")
    if [ ${#missing[@]} -gt 0 ]; then
        fail "Missing required .env variables: ${missing[*]}"
    fi

    # Create data directories
    mkdir -p "$DATA_DIR" "$REDIS_DATA_DIR"
    log "Data directories ready."

    # Pull / build
    if [ "$rebuild" = "--rebuild" ]; then
        log "Force-rebuilding images..."
        compose build --no-cache
    else
        log "Building images..."
        compose build
    fi

    # Restart
    log "Starting containers..."
    compose up -d --remove-orphans

    log ""
    log "Deployment complete!"
    log "  View logs:  ./setup.sh --logs"
    log "  Stop:       ./setup.sh --stop"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

case "${1:-}" in
    --stop)    cmd_stop ;;
    --logs)    cmd_logs ;;
    --rebuild) cmd_deploy "--rebuild" ;;
    *)         cmd_deploy ;;
esac
