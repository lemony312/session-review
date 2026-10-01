# Development

`install.sh` symlinks `commands/review-ui.md` into `~/.claude/commands/` and starts the server on `http://localhost:8087` (override with `SESSION_REVIEW_PORT`). If you clone somewhere other than `~/Documents/session-review`, set `SESSION_REVIEW_HOME` to the clone path so the command can find the health script.

## Configuration

The container mounts these read-only (see `docker-compose.yml`):

| Mount | Purpose |
|-------|---------|
| `~/.claude` | Session transcripts for annotations |
| `$REPOS_HOST_DIR` (default `~/Documents`) | Repos to diff, mounted at `/repos`. **Repos must live under it** |

Environment variables. "Host" variables are read by `scripts/session-review-health.sh` and the Ask bridge; "container" variables are set in `docker-compose.yml` and read by the server.

| Variable | Read by | Default | What it does |
|----------|---------|---------|--------------|
| `REPOS_HOST_DIR` | host (health script, compose, bridge); container (path mapping) | `~/Documents` | Host directory holding the repos. Mounted at `/repos`; also the only tree the bridge will run the agent in. Restart the bridge after changing it. |
| `REPOS_DIR` | container | `/repos` | Where the repos are mounted inside the container. |
| `SESSION_REVIEW_HOME` | `/review-ui` command, `install.sh` | `~/Documents/session-review` | Clone location, used to find `scripts/session-review-health.sh`. |
| `ASK_AGENT` | bridge | `claude` | Agent preset: `claude` or `kiro`. See [Ask with other agents](#ask-with-other-agents). |
| `ASK_AGENT_CMD` | bridge | unset | Any other agent CLI, as a shell-words string (never run through a shell). Overrides `ASK_AGENT`. |
| `ASK_AGENT_NAME` | bridge | preset name, or basename of the custom binary | Display name in the UI ("Ask <name>"). |
| `ASK_BRIDGE_PORT` | host (bridge, health script) | `8095` | Port the bridge listens on. |
| `ASK_BRIDGE_HOST` | host (bridge, health script) | `127.0.0.1`; on Linux the health script uses the Docker bridge gateway (from `docker network inspect bridge`, fallback `172.17.0.1`) | Bridge bind address. Never `0.0.0.0` by default. |
| `ASK_BRIDGE_URL` | container | `http://host.docker.internal:$ASK_BRIDGE_PORT` (port 8095 if unset) | Where the container reaches the bridge. The health script exports `ASK_BRIDGE_PORT` to compose, so changing the port needs no URL change. |
| `DOMAIN_CONTEXT_DIR` | container | unset (skipped) | Container path of a directory of `*.md` files. For review-scope Ask, files whose name contains the repo name are added to the prompt as domain context. Must be under the `/repos` mount to be visible. |
| `ASK_BRIDGE_ROOT` | bridge | `REPOS_HOST_DIR`, else `~/Documents` | Directory the bridge will accept as an agent working directory. Anything outside is refused. |
| `PIPELINE_IMAGE_REGISTRY_PREFIXES` | container (via compose) | unset | Comma-separated image registry prefixes (e.g. `registry.example.com/team/,mirror.example.com/`). A pipeline stage image that starts with none of them gets a critical `nonmirror-image` lint flag; images built from SpEL are skipped. Unset or empty disables the check. Set it in the shell that runs the health script, then `--stop` and start again. |
| `DB_PATH` | container | `/data/review.db` | SQLite review database. |
| `REPO_CONFIG_PATH` | container | `/data/repo_config.json` | Per-repo settings file. |
| `CLAUDE_DIR` | container | `/claude` | Where `~/.claude` is mounted; session transcripts are read from it. |
| `CLAUDE_HOST_DIR` | container | `~/.claude` | Host path of the same directory, for display and path mapping. |
| `XDG_STATE_HOME` | health script (Linux) | `~/.local/state` | Base for the bridge log directory. |
| `SESSION_REVIEW_PORT` | compose, health script, `/review-ui` | `8087` | Host port for the server, bound to `127.0.0.1` only (not reachable from the network). The container always listens on 8087 internally. |
| `SESSION_REVIEW_CONTAINER` | compose, health script | `session-review` | Container name and compose project name. Use a different value with a different port to run a second stack. |

To review repos elsewhere, set `REPOS_HOST_DIR` (e.g. `REPOS_HOST_DIR=~/src scripts/session-review-health.sh --rebuild`). The health script exports it to both docker-compose and the bridge. A running bridge keeps the root it started with (`start_bridge` returns early if the bridge is up, and `--rebuild` doesn't stop it), so after changing it run `--stop`, then start again.

## Ask

"Ask" runs an agent CLI on the host, so it uses your existing login (no AWS/Bedrock). The default is `claude`. The container can't see host CLIs, so `scripts/ask-bridge.py` (stdlib only) runs on the host and the container calls it at `ASK_BRIDGE_URL`.

- The bridge runs one fixed command (below) with the repo as cwd (must be under `REPOS_HOST_DIR`, or `ASK_BRIDGE_ROOT` if set), a 180s timeout, and the prompt on stdin. Every prompt is prefixed with: "This is a question only. Answer it by reading the code; do not modify, create or delete any files and do not run commands."
- Read-only, not read-confined: the cwd is only where the agent starts. Claude Code's Read/Grep/Glob can open any file your user can, e.g. other repos or `~/.aws`; there is no write, shell or network tool to act on it. To confine reads to the repo, add `--restricted` to the claude command and pass the login keys from `~/.claude/settings.json` (`env`, `awsCredentialExport`) via `--settings`, because `--restricted` ignores user settings.
- `GET /health` on the bridge returns `{available, agent, name, binary, version?, claude}`; the UI reads it through `GET /api/ask/status` and labels the button "Ask Claude", "Ask Kiro", and so on.
- `scripts/session-review-health.sh` starts the bridge (pid: `data/ask-bridge.pid`) and `--stop` stops it. Log: `~/Library/Logs/session-review-ask-bridge.log` on macOS, `${XDG_STATE_HOME:-~/.local/state}/session-review/session-review-ask-bridge.log` on Linux.
- Without the bridge or the agent CLI, `/api/ask` returns `code: claude_not_available` (kept for compatibility, whichever agent is configured), the message reads "Ask needs the <name> CLI (`<binary>`) on this machine.", and the UI greys out Ask. The diff view is unaffected.
- Session notes are stored in full up to 4000 chars (`MAX_ANNOTATION_LEN`); the UI note card scrolls.

## Ask with other agents

Choose the agent with `ASK_AGENT` or `ASK_AGENT_CMD` in the environment of the process that starts the bridge (`ASK_AGENT=kiro scripts/session-review-health.sh`; restart the bridge after changing it: `--stop`, then start).

**Presets**

| Preset | Command | Read-only mechanism |
|--------|---------|---------------------|
| `claude` (default) | `claude -p --output-format json --allowedTools Read,Grep,Glob --disallowedTools Edit,Write,Bash,NotebookEdit,WebFetch [--model opus\|sonnet\|haiku]` | Tool allow and deny lists |
| `kiro` | `kiro-cli chat --no-interactive --wrap never --trust-tools=fs_read,grep,glob` | Only these three tools are trusted; in `--no-interactive` mode every untrusted tool (`fs_write`, `execute_bash`, ...) is denied because nobody can approve it |

The prompt goes on stdin for both. The opus/sonnet/haiku picker applies only to `claude`; other agents ignore it. For `kiro` the bridge strips ANSI codes and the tool-call trace kiro prints on stdout and returns just the answer.

**Custom command (`ASK_AGENT_CMD`)**

```bash
ASK_AGENT_CMD='my-agent --print' scripts/session-review-health.sh
ASK_AGENT_CMD='my-agent --ask {prompt}' ASK_AGENT_NAME='My Agent' scripts/session-review-health.sh
```

- The string is split with shell-words rules (`shlex`) and run directly, never through a shell, so `$(...)`, `;` and pipes are literal arguments.
- The prompt goes on stdin and stdout is the answer. If an argument contains `{prompt}`, the prompt is substituted there instead, as one argv element, and nothing is sent on stdin. Very large prompts can exceed the OS argument limit (about 128 KB per argument on Linux), so prefer stdin.
- A non-zero exit or empty stdout is reported as an error.
- The model picker is ignored.

> **For custom commands the read-only preamble is the only guard.** The bridge prepends "answer by reading the code; do not modify files or run commands" to the prompt, but nothing enforces it. Add your tool's own read-only or sandbox flag to `ASK_AGENT_CMD`.

Other CLIs (codex, gemini, cursor-agent, aider, opencode, copilot, amp, goose) can be used this way, but none is built in or tested here. Look up that tool's read-only or sandbox option yourself and put it in the command.

**Linux**

- Add `host.docker.internal:host-gateway` (already in `docker-compose.yml`) so the container can reach the host. On Linux that resolves to the docker0 gateway, not loopback, so a loopback-only bridge would be unreachable.
- On Linux, when `ASK_BRIDGE_HOST` is unset, the health script binds the bridge to the docker0 gateway (`docker network inspect bridge`, falling back to `172.17.0.1`), not to `0.0.0.0`. Other machines on the network cannot reach it. Set `ASK_BRIDGE_HOST` to override.
- Bridge log: `${XDG_STATE_HOME:-~/.local/state}/session-review/session-review-ask-bridge.log`.
- A host firewall must allow the container subnet to reach the bridge port on the docker0 address.

## Server lifecycle

```bash
scripts/session-review-health.sh            # check health, start if down
scripts/session-review-health.sh --check    # exit 0 = healthy, 1 = down
scripts/session-review-health.sh --stop
scripts/session-review-health.sh --logs
scripts/session-review-health.sh --rebuild  # after pulling changes
```

Generating a review without Claude Code:

```bash
curl -sG -X POST http://localhost:8087/api/reviews/generate-branch \
  --data-urlencode "repo_path=$HOME/Documents/<repo>" \
  --data-urlencode "branch=<branch>" --data-urlencode "base_ref=origin/main"
open http://localhost:8087/review/<review_id>
```

## Tests

```bash
uv run python test_pipeline_diff.py   # pipeline diff payloads (synthetic fixtures)
uv run python test_ask_bridge.py      # ask-bridge agents/presets, path mapping, not-available handling
uv run python test_review_generator.py  # repo-path resolution (clones/worktrees), worktree diffs, annotation path normalisation
uv run python test_annotation_determinism.py  # note placement doesn't depend on PYTHONHASHSEED
uv run python test_jsonl_parser.py    # every assistant record (after tool calls too) reaches a turn
node test_pipeline_render.js          # rendered HTML shows the embedded shell (synthetic fixtures)
node test_markdown_diff.js            # rendered-markdown diff (self-contained)
node test_split_diff.js               # split diff row alignment + HTML (self-contained)
node test_inline_expand.js            # Inline/Traditional "Show N more lines" ranges (self-contained)
```

`test_pipeline_diff.py` and `test_pipeline_render.js` run against synthetic before/after pairs in `tests/fixtures/pipelines/` (`<case>.old.pp` / `<case>.new.pp`): embedded shell edits, image/env changes, stages added and removed, a trigger added, a templated-pipeline version bump and a CronJob under `manifests[]`. No git clones or network are needed. To add a case, drop a new pair there and assert on it.

To work on the pipeline renderer without the server, `preview_pipeline_diff.py <old.pp> <new.pp> --open` renders one `.pp` diff from two files (or `--git <repo> <commit> <path>` from git blobs) using the real CSS and JS.
