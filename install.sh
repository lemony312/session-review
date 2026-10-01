#!/usr/bin/env bash
set -euo pipefail

# Install the /review-ui Claude Code command and start the session-review server.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
COMMANDS_DIR="$HOME/.claude/commands"

mkdir -p "$COMMANDS_DIR"
ln -sf "$REPO_DIR/commands/review-ui.md" "$COMMANDS_DIR/review-ui.md"
echo "Linked $COMMANDS_DIR/review-ui.md -> $REPO_DIR/commands/review-ui.md"

if [[ "$REPO_DIR" != "$HOME/Documents/session-review" ]]; then
    echo ""
    echo "NOTE: repo is not at ~/Documents/session-review. Add this to your shell profile:"
    echo "  export SESSION_REVIEW_HOME=\"$REPO_DIR\""
fi

echo ""
echo "NOTE: repos must live under ~/Documents. If yours are elsewhere, add this to your shell profile:"
echo "  export REPOS_HOST_DIR=\"/path/to/repos\""

"$REPO_DIR/scripts/session-review-health.sh"
