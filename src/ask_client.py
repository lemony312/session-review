"""Container-side client for the host ask-bridge (see scripts/ask-bridge.py)."""
from __future__ import annotations

import json
import os
import urllib.request

BRIDGE_URL = lambda: os.environ.get("ASK_BRIDGE_URL", "http://host.docker.internal:8095").rstrip("/")

# Last agent the bridge reported; used to word the message when the bridge itself is unreachable.
_last_agent: dict = {"agent": "claude", "name": "Claude", "binary": "claude"}


def not_available_message(info: dict | None = None) -> str:
    info = info or _last_agent
    return f"Ask needs the {info.get('name') or 'Claude'} CLI (`{info.get('binary') or 'claude'}`) on this machine."


NOT_AVAILABLE_MSG = not_available_message()  # default (claude) wording


def not_available(info: dict | None = None) -> dict:
    # code stays claude_not_available for compat with the UI and older bridges
    return {"error": True, "code": "claude_not_available", "response": not_available_message(info)}


def to_host_path(path: str) -> str:
    """Container path (/repos/x) -> host path; host paths pass through."""
    repos_dir = os.path.normpath(os.environ.get("REPOS_DIR", "/repos"))
    host_dir = os.environ.get("REPOS_HOST_DIR", "")
    p = os.path.normpath(path)
    if host_dir and (p == repos_dir or p.startswith(repos_dir + "/")):
        return os.path.normpath(host_dir) + p[len(repos_dir):]
    return path


def bridge_info(timeout: float = 3) -> dict | None:
    """The bridge's /health payload ({available, agent, name, binary, ...}), or None if unreachable."""
    try:
        with urllib.request.urlopen(f"{BRIDGE_URL()}/health", timeout=timeout) as r:
            d = json.load(r)
    except Exception:
        return None
    if "available" not in d:  # bridge from before pluggable agents: {"claude": bool}
        d = {"available": bool(d.get("claude")), "agent": "claude", "name": "Claude", "binary": "claude"}
    if d.get("name"):
        _last_agent.update({k: d[k] for k in ("agent", "name", "binary") if d.get(k)})
    return d


def bridge_status() -> bool:
    """True if the bridge answers and its agent CLI is installed."""
    info = bridge_info()
    return bool(info and info.get("available"))


def ask_claude(repo_path: str, prompt: str, model: str | None) -> dict:
    """Returns {'answer', 'agent', 'name'} on success, else a response dict with error/code/response."""
    info = bridge_info()
    if not info or not info.get("available"):
        return not_available(info)
    body = json.dumps({"cwd": to_host_path(repo_path), "prompt": prompt, "model": model}).encode()
    req = urllib.request.Request(f"{BRIDGE_URL()}/ask", data=body, method="POST",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            d = json.load(e)
        except Exception:
            d = {}
        if d.get("code") in ("claude_not_available", "agent_not_available"):
            return not_available(d)
        return {"error": True, "code": d.get("code", "bridge_error"),
                "response": f"Ask failed: {d.get('message', e)}"}
    except Exception:
        return not_available()
