#!/usr/bin/env bash
set -euo pipefail

# Manual install (no plugin): link the review-ui skill into ~/.claude/skills and start the server.
# Prefer the plugin route; see README.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="$HOME/.claude/skills"

mkdir -p "$SKILLS_DIR"
ln -sfn "$REPO_DIR/skills/review-ui" "$SKILLS_DIR/review-ui"
echo "Linked $SKILLS_DIR/review-ui -> $REPO_DIR/skills/review-ui"

# Older installs linked commands/review-ui.md, which no longer exists.
OLD="$HOME/.claude/commands/review-ui.md"
if [[ -L "$OLD" && "$(readlink "$OLD")" == "$REPO_DIR/commands/review-ui.md" ]]; then
    rm "$OLD"
    echo "Removed stale $OLD"
fi

if [[ "$REPO_DIR" != "$HOME/Documents/session-review" ]]; then
    echo ""
    echo "NOTE: repo is not at ~/Documents/session-review. Add this to your shell profile:"
    echo "  export SESSION_REVIEW_HOME=\"$REPO_DIR\""
fi

echo ""
echo "NOTE: repos must live under ~/Documents. If yours are elsewhere, add this to your shell profile:"
echo "  export REPOS_HOST_DIR=\"/path/to/repos\""

"$REPO_DIR/scripts/session-review-health.sh"
