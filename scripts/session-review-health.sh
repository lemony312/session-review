#!/usr/bin/env bash
set -euo pipefail

# Session Review health check / lifecycle manager

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Host port and container name (also the compose project name), so several stacks can coexist.
# The container always listens on 8087 internally.
PORT="${SESSION_REVIEW_PORT:-8087}"
CONTAINER_NAME="${SESSION_REVIEW_CONTAINER:-session-review}"
export SESSION_REVIEW_PORT="$PORT" SESSION_REVIEW_CONTAINER="$CONTAINER_NAME"
IMAGE="session-review:latest"
URL="http://localhost:$PORT"

# Host directory holding the repos to review: mounted at /repos (docker-compose.yml)
# and the only tree the ask bridge will run claude in.
export REPOS_HOST_DIR="${REPOS_HOST_DIR:-$HOME/Documents}"

# Host bridge that lets the container use the host's agent CLI (Ask; claude by default, see ASK_AGENT)
BRIDGE_PORT="${ASK_BRIDGE_PORT:-8095}"
export ASK_BRIDGE_PORT="$BRIDGE_PORT"  # compose derives the container's ASK_BRIDGE_URL from it
BRIDGE_PID="$PROJECT_DIR/data/ask-bridge.pid"

# Bind address: loopback on macOS (Docker Desktop forwards host.docker.internal there).
# On Linux, host.docker.internal is host-gateway = the docker0 gateway, so bind exactly that
# (never 0.0.0.0). ASK_BRIDGE_HOST overrides.
if [ -z "${ASK_BRIDGE_HOST:-}" ]; then
    if [ "$(uname)" = "Linux" ]; then
        ASK_BRIDGE_HOST="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
        ASK_BRIDGE_HOST="${ASK_BRIDGE_HOST:-172.17.0.1}"
    else
        ASK_BRIDGE_HOST="127.0.0.1"
    fi
fi
export ASK_BRIDGE_HOST
BRIDGE_URL="http://$ASK_BRIDGE_HOST:$BRIDGE_PORT"

if [ "$(uname)" = "Darwin" ]; then
    LOG_DIR="$HOME/Library/Logs"
else
    LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/session-review"
fi
BRIDGE_LOG="$LOG_DIR/session-review-ask-bridge.log"

usage() {
    echo "Usage: $0 [--check|--stop|--logs|--rebuild]"
    echo ""
    echo "  (no args)   Check health, auto-start if down"
    echo "  --check     Check only (exit 0 = healthy, 1 = down)"
    echo "  --stop      Stop the container"
    echo "  --logs      Tail container logs"
    echo "  --rebuild   Rebuild image from scratch and restart"
    echo ""
}

is_healthy() {
    curl -sf "$URL/health" > /dev/null 2>&1
}

bridge_up() {
    curl -sf "$BRIDGE_URL/health" > /dev/null 2>&1
}

start_bridge() {
    if bridge_up; then return 0; fi
    mkdir -p "$PROJECT_DIR/data" "$(dirname "$BRIDGE_LOG")"
    ASK_BRIDGE_PORT="$BRIDGE_PORT" ASK_BRIDGE_HOST="$ASK_BRIDGE_HOST" nohup python3 "$PROJECT_DIR/scripts/ask-bridge.py" >> "$BRIDGE_LOG" 2>&1 &
    echo $! > "$BRIDGE_PID"
    for i in $(seq 1 10); do bridge_up && { echo "ask-bridge listening on $BRIDGE_URL"; return 0; }; sleep 0.5; done
    echo "WARNING: ask-bridge failed to start (see $BRIDGE_LOG); Ask will be unavailable"
}

stop_bridge() {
    if [ -f "$BRIDGE_PID" ]; then
        kill "$(cat "$BRIDGE_PID")" 2>/dev/null || true
        rm -f "$BRIDGE_PID"
        echo "ask-bridge stopped."
    fi
}

start_container() {
    start_bridge
    echo "Starting $CONTAINER_NAME container on port $PORT..."
    cd "$PROJECT_DIR"

    # Build if image doesn't exist
    if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
        echo "Building session-review image..."
        docker compose build
    fi

    docker compose up -d
    echo "Waiting for container to be healthy..."

    for i in $(seq 1 30); do
        if is_healthy; then
            echo "session-review is healthy on port $PORT"
            curl -sf "$URL/health" | python3 -m json.tool 2>/dev/null || true
            return 0
        fi
        sleep 1
    done

    echo "ERROR: session-review failed to start within 30s"
    docker compose logs --tail=20
    return 1
}

case "${1:-}" in
    --check)
        if is_healthy; then
            echo "healthy"
            exit 0
        else
            echo "down"
            exit 1
        fi
        ;;
    --stop)
        echo "Stopping session-review..."
        cd "$PROJECT_DIR"
        docker compose down
        stop_bridge
        echo "Stopped."
        ;;
    --logs)
        docker logs -f "$CONTAINER_NAME" 2>&1
        ;;
    --rebuild)
        echo "Rebuilding session-review image..."
        cd "$PROJECT_DIR"
        docker compose down 2>/dev/null || true
        docker compose build --no-cache
        start_container
        ;;
    -h|--help)
        usage
        ;;
    "")
        if is_healthy; then
            start_bridge
            echo "session-review is healthy on port $PORT"
            curl -sf "$URL/health" | python3 -m json.tool 2>/dev/null || true
        else
            start_container
        fi
        ;;
    *)
        echo "Unknown option: $1"
        usage
        exit 1
        ;;
esac
