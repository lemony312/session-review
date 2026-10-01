---
description: Open a branch or pull request in the session-review diff viewer (localhost:8087). Use when the user says "show me the review of …", "review this branch/PR", "open the review", or pastes a PR link asking to review it.
argument-hint: "[PR url | PR number | branch | base..branch] [--base <ref>]"
---

Open a GitHub-style review of a branch or pull request at `http://localhost:${SESSION_REVIEW_PORT:-8087}` (below: `$URL`).

1. **Start the server**: run `"${SESSION_REVIEW_HOME:-$HOME/Documents/session-review}/scripts/session-review-health.sh"`. It is idempotent and starts the server and Ask bridge if they are down.

2. **Pick repo, branch and base** from $ARGUMENTS:

   | Argument | Branch | Base |
   |---|---|---|
   | none | current branch (`git branch --show-current`, or `git rev-parse --short HEAD` if detached) | default branch |
   | `<branch>` | `<branch>` | default branch |
   | `<base>..<branch>` | `<branch>` | `<base>` |
   | `--base <ref>` | (as above) | `<ref>` |
   | PR url, `#<n>` or `<n>` | see below | see below |

   - **Repo**: `git rev-parse --show-toplevel`. In a worktree (including `.claude/worktrees/`) use the worktree path as is. It must be under `REPOS_HOST_DIR` (default `~/Documents`).
   - **Default branch**: `origin/<name>` from `git symbolic-ref refs/remotes/origin/HEAD` (fallback `origin/main`). With no `origin`, local `main`, else `master`.
   - If the branch is the base, say there is nothing to review and stop.
   - **Pull request** (any GitHub host, `https://<host>/<owner>/<repo>/pull/<n>`; `gh` must be logged in to that host): `gh pr view <url-or-n> --json number,headRefName,baseRefName,isCrossRepository`. For a URL, use the current repo if its `origin` matches `<owner>/<repo>`, else the clone under `REPOS_HOST_DIR` whose origin matches (none: ask the user to clone it there, and stop). Then:
     - same repo: `git fetch origin <head> <base>`; branch `origin/<head>`
     - fork: `git fetch origin pull/<n>/head:refs/remotes/origin/pr/<n> <base>`; branch `origin/pr/<n>`
     - base `origin/<baseRefName>`
   - Pass refs by name (e.g. `origin/main`), not SHAs; the server does the three-dot diff itself.

3. **Session notes (optional)**: `curl -s $URL/api/branches` → `branches[]` with `repo_path`, `branch`, `sessions[]` (`session_id`, `first_prompt`). Take the entry matching repo and branch (in a worktree, try the worktree path, then the main repo: `dirname "$(git rev-parse --path-format=absolute --git-common-dir)"`). Drop sessions whose `first_prompt` contains `<command-name>/review-ui</command-name>`. Skip this for PRs unless a local branch of that name has sessions.

4. **Generate**:
   ```
   curl -sG -X POST "$URL/api/reviews/generate-branch" \
     --data-urlencode "repo_path=<REPO>" --data-urlencode "branch=<BRANCH>" --data-urlencode "base_ref=<BASE>" \
     [--data-urlencode "session_ids=<ID1>,<ID2>"]
   ```
   Expect `{"review_id": <id>, "status": "generated"|"unchanged"}`; otherwise report `detail`.

5. **Open**: `open "$URL/review/<review_id>"`.

6. **Report** in 2–3 lines: the URL, `<branch> vs <base>`, and the file count (`curl -s $URL/api/reviews/<id>` → `.review.total_files_changed`). Re-running refreshes it. Stick to facts about the diff.
