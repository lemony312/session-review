#!/usr/bin/env python3
"""Host bridge that lets the session-review container use an agent CLI on the host.

The container has no agent auth; the host does. This server runs one fixed command
only, chosen by env: the `claude` preset (default), the `kiro` preset, or a custom
ASK_AGENT_CMD. Stdlib only.

  GET  /health -> {available, agent, name, binary, version?, claude}
  POST /ask    -> {cwd, prompt, model} => {answer, model, agent, name} | {error, code, message}

Env: ASK_AGENT (claude|kiro), ASK_AGENT_CMD (shell-words command; overrides ASK_AGENT),
ASK_AGENT_NAME (display name), ASK_BRIDGE_PORT (8095), ASK_BRIDGE_HOST (127.0.0.1),
ASK_BRIDGE_ROOT / REPOS_HOST_DIR (cwd must be inside).

Default bind is 127.0.0.1: Docker Desktop for Mac forwards host.docker.internal to the
host's loopback. On Linux, session-review-health.sh binds the docker0 gateway instead.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

READ_ONLY_PREAMBLE = (
    "This is a question only. Answer it by reading the code; "
    "do not modify, create or delete any files and do not run commands."
)
ALLOWED_TOOLS = ["Read", "Grep", "Glob"]
DISALLOWED_TOOLS = ["Edit", "Write", "Bash", "NotebookEdit", "WebFetch"]
KIRO_TRUSTED_TOOLS = ["fs_read", "grep", "glob"]  # read-only; everything else is denied in --no-interactive
MODELS = {"opus", "sonnet", "haiku"}
TIMEOUT_SECONDS = 180
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8095
# cwd must be inside the repos tree the server mounts (REPOS_HOST_DIR); ASK_BRIDGE_ROOT overrides
ALLOWED_ROOT = Path(os.environ.get("ASK_BRIDGE_ROOT") or os.environ.get("REPOS_HOST_DIR")
                    or Path.home() / "Documents").expanduser().resolve()

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# kiro prints tool traces on stdout; the answer follows the last one
KIRO_TRACE_END_RE = re.compile(r"^\s*(?:- Completed in [\d.]+m?s|- non-interactive mode \(no user to approve\))\s*$"
                               r"|\(using tool: \w+\)\s*$")


class AgentConfigError(ValueError):
    pass


def strip_ansi(text: str) -> str:
    text = ANSI_RE.sub("", text)
    # spinners redraw the line with \r; keep the last redraw
    return "\n".join(line.split("\r")[-1] for line in text.split("\n"))


def parse_claude(stdout: str, returncode: int) -> str:
    out = json.loads(stdout)  # ValueError -> caller reports raw output
    if out.get("is_error") or returncode != 0:
        raise RuntimeError(str(out.get("result", ""))[:500])
    return out.get("result", "")


def parse_kiro(stdout: str, returncode: int) -> str:
    lines = strip_ansi(stdout).split("\n")
    last_trace = -1
    for i, line in enumerate(lines):
        if KIRO_TRACE_END_RE.search(line):
            last_trace = i
    text = "\n".join(lines[last_trace + 1:]).strip()
    if text.startswith("> "):
        text = text[2:]
    text = text.strip()
    if returncode != 0 or not text:
        raise RuntimeError((text or strip_ansi(stdout)).strip()[:500] or "no output")
    return text


def parse_plain(stdout: str, returncode: int) -> str:
    text = stdout.strip()
    if returncode != 0 or not text:
        raise RuntimeError(text[:500] or "no output")
    return text


class Agent:
    def __init__(self, key: str, name: str, binary: str, args: list[str],
                 parse: Callable[[str, int], str], supports_model: bool = False,
                 prompt_in_arg: bool = False, version_args: list[str] | None = ("--version",)):
        self.key = key                  # preset name, or "custom"
        self.name = name                # display name
        self.binary = binary            # executable as configured
        self.args = list(args)          # argv after the binary
        self.parse = parse              # (stdout, returncode) -> answer; raises RuntimeError on failure
        self.supports_model = supports_model
        self.prompt_in_arg = prompt_in_arg  # custom {prompt} substitution instead of stdin
        self.version_args = list(version_args) if version_args else None

    def build_command(self, prompt: str, model: str | None, path: str | None = None) -> tuple[list[str], str | None]:
        """Return (argv, stdin_text|None). Prompt (with the read-only preamble) goes on
        stdin unless the custom command has a {prompt} placeholder."""
        full = f"{READ_ONLY_PREAMBLE}\n\n{prompt}"
        args = list(self.args)
        if self.supports_model and model in MODELS:
            args += ["--model", model]
        if self.prompt_in_arg:
            return [path or self.binary] + [a.replace("{prompt}", full) for a in args], None
        return [path or self.binary] + args, full


def _claude_args() -> list[str]:
    # Prompt on stdin: it can be ~1MB, and the variadic --allowedTools would swallow a trailing positional.
    return ["-p", "--output-format", "json",
            "--allowedTools", ",".join(ALLOWED_TOOLS),
            "--disallowedTools", ",".join(DISALLOWED_TOOLS)]


PRESETS: dict[str, Callable[[], Agent]] = {
    "claude": lambda: Agent("claude", "Claude", "claude", _claude_args(), parse_claude, supports_model=True),
    "kiro": lambda: Agent("kiro", "Kiro", "kiro-cli",
                          ["chat", "--no-interactive", "--wrap", "never",
                           "--trust-tools=" + ",".join(KIRO_TRUSTED_TOOLS)],
                          parse_kiro),
}


def resolve_agent(env=None) -> Agent:
    """ASK_AGENT_CMD (custom, shlex-parsed, never a shell) overrides ASK_AGENT (preset)."""
    env = os.environ if env is None else env
    cmd = (env.get("ASK_AGENT_CMD") or "").strip()
    display = (env.get("ASK_AGENT_NAME") or "").strip()
    if cmd:
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            raise AgentConfigError(f"ASK_AGENT_CMD is not valid shell-words: {e}")
        if not argv:
            raise AgentConfigError("ASK_AGENT_CMD is empty")
        binary = argv[0]
        return Agent("custom", display or os.path.basename(binary), binary, argv[1:], parse_plain,
                     prompt_in_arg=any("{prompt}" in a for a in argv[1:]), version_args=None)
    key = (env.get("ASK_AGENT") or "claude").strip().lower()
    if key not in PRESETS:
        raise AgentConfigError(f"unknown ASK_AGENT {key!r}; choose one of: {', '.join(sorted(PRESETS))} "
                               "(or set ASK_AGENT_CMD for any other CLI)")
    agent = PRESETS[key]()
    if display:
        agent.name = display
    return agent


def build_command(prompt: str, model: str | None, claude_path: str = "claude") -> tuple[list[str], str]:
    """Claude preset argv + stdin (kept for callers/tests that predate pluggable agents)."""
    return PRESETS["claude"]().build_command(prompt, model, claude_path)


def bind_host() -> str:
    return os.environ.get("ASK_BRIDGE_HOST") or DEFAULT_HOST


def not_available_message(agent: Agent) -> str:
    return f"Ask needs the {agent.name} CLI (`{agent.binary}`) on this machine."


def agent_version(agent: Agent) -> str | None:
    path = shutil.which(agent.binary)
    if not path or not agent.version_args:
        return None
    try:
        r = subprocess.run([path, *agent.version_args], capture_output=True, text=True, timeout=15)
        return r.stdout.strip() or None
    except Exception:
        return None


def health() -> dict:
    try:
        agent = resolve_agent()
    except AgentConfigError as e:
        return {"available": False, "agent": None, "name": None, "claude": False, "error": str(e)}
    available = shutil.which(agent.binary) is not None
    out = {"available": available, "agent": agent.key, "name": agent.name, "binary": agent.binary,
           "claude": available and agent.key == "claude"}
    version = agent_version(agent) if available else None
    if version:
        out["version"] = version
    return out


def run_ask(cwd: str, prompt: str, model: str | None) -> tuple[int, dict]:
    try:
        agent = resolve_agent()
    except AgentConfigError as e:
        return 500, {"error": True, "code": "bad_config", "message": str(e)}
    who = {"agent": agent.key, "name": agent.name, "binary": agent.binary}
    path = shutil.which(agent.binary)
    if not path:
        # code kept as claude_not_available for compat with older containers/UI
        return 503, {"error": True, "code": "claude_not_available", "message": not_available_message(agent), **who}
    try:
        resolved = Path(cwd).resolve()
    except Exception:
        resolved = None
    if not resolved or not resolved.is_dir() or (resolved != ALLOWED_ROOT and ALLOWED_ROOT not in resolved.parents):
        return 400, {"error": True, "code": "bad_cwd", "message": f"cwd must be an existing directory under {ALLOWED_ROOT}"}
    argv, stdin_text = agent.build_command(prompt, model, path)
    try:
        r = subprocess.run(argv, input=stdin_text, cwd=str(resolved), capture_output=True,
                           text=True, timeout=TIMEOUT_SECONDS, shell=False)
    except subprocess.TimeoutExpired:
        return 504, {"error": True, "code": "timeout", "message": f"{agent.name} timed out after {TIMEOUT_SECONDS}s", **who}
    except OSError as e:
        return 502, {"error": True, "code": "agent_failed", "message": f"could not run {agent.binary}: {e}", **who}
    try:
        answer = agent.parse(r.stdout, r.returncode)
    except ValueError:
        return 502, {"error": True, "code": "agent_failed",
                     "message": strip_ansi(r.stderr or r.stdout or "no output").strip()[:500], **who}
    except RuntimeError as e:
        msg = str(e) or strip_ansi(r.stderr).strip()[:500] or "no output"
        return 502, {"error": True, "code": "agent_failed", "message": msg, **who}
    return 200, {"answer": answer, "model": model if agent.supports_model else None, **who}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, health())
        else:
            self._send(404, {"error": True})

    def do_POST(self):
        if self.path != "/ask":
            return self._send(404, {"error": True})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
            cwd, prompt = str(body["cwd"]), str(body["prompt"])
        except Exception:
            return self._send(400, {"error": True, "code": "bad_request", "message": "need cwd and prompt"})
        self._send(*run_ask(cwd, prompt, body.get("model")))

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    try:
        agent = resolve_agent()
    except AgentConfigError as e:
        sys.exit(f"ask-bridge: {e}")
    port = int(os.environ.get("ASK_BRIDGE_PORT", str(DEFAULT_PORT)))
    host = bind_host()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"ask-bridge listening on {host}:{port} (agent: {agent.name}, `{agent.binary}`)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
