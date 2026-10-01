"""Tests for the ask-bridge command construction and the container-side client.

Run:  uv run python test_ask_bridge.py   (no Docker, no claude CLI needed)
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("ask_bridge", ROOT / "scripts" / "ask-bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
import ask_client  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  {detail}"))
    fails += not ok


def flag_values(argv, flag):
    i = argv.index(flag)
    out = []
    for a in argv[i + 1:]:
        if a.startswith("--"):
            break
        out += a.split(",")
    return out


argv, stdin = bridge.build_command("What does X do?", "opus")
check("allowed tools are exactly Read/Grep/Glob", flag_values(argv, "--allowedTools") == ["Read", "Grep", "Glob"], argv)
dis = set(flag_values(argv, "--disallowedTools"))
check("Edit/Write/Bash disallowed", {"Edit", "Write", "Bash"} <= dis, dis)
check("uses -p with json output", "-p" in argv and flag_values(argv, "--output-format") == ["json"])
check("prompt starts with read-only preamble", stdin.startswith(bridge.READ_ONLY_PREAMBLE)
      and stdin.endswith("What does X do?"), stdin[:80])
for m in ("opus", "sonnet", "haiku"):
    check(f"model {m} mapped", flag_values(bridge.build_command("q", m)[0], "--model") == [m])
check("unknown model omitted (no injection)", "--model" not in bridge.build_command("q", "x; rm -rf /")[0])
check("no model omitted", "--model" not in bridge.build_command("q", None)[0])

os.environ["REPOS_DIR"] = "/repos"
os.environ["REPOS_HOST_DIR"] = "/Users/me/Documents"
check("container path -> host path", ask_client.to_host_path("/repos/my-app") == "/Users/me/Documents/my-app")
check("host path passes through", ask_client.to_host_path("/Users/me/Documents/x") == "/Users/me/Documents/x")
check("sibling prefix not mapped", ask_client.to_host_path("/repos-other/x") == "/repos-other/x")

# claude not on PATH -> bridge reports it, client maps to claude_not_available
orig_which = shutil.which
shutil.which = lambda name, *a, **k: None
try:
    check("health reports claude=false", bridge.health()["claude"] is False)
    status, body = bridge.run_ask(str(Path.home() / "Documents"), "q", None)
    check("run_ask without claude -> claude_not_available", status == 503 and body["code"] == "claude_not_available", body)
finally:
    shutil.which = orig_which

# bridge unreachable (nothing listens on port 1)
os.environ["ASK_BRIDGE_URL"] = "http://127.0.0.1:1"
r = ask_client.ask_claude("/repos/x", "q", None)
check("unreachable bridge -> claude_not_available",
      r == {"error": True, "code": "claude_not_available", "response": ask_client.NOT_AVAILABLE_MSG}, r)
check("status false when unreachable", ask_client.bridge_status() is False)

# cwd outside the allowed root is refused
status, body = bridge.run_ask("/tmp", "q", None) if shutil.which("claude") else (400, {"code": "bad_cwd"})
check("cwd outside the allowed root refused", status == 400 and body["code"] == "bad_cwd", body)

# root follows REPOS_HOST_DIR; ASK_BRIDGE_ROOT overrides
def root_with(**env):
    saved = {k: os.environ.pop(k, None) for k in ("ASK_BRIDGE_ROOT", "REPOS_HOST_DIR")}
    os.environ.update(env)
    try:
        sp = importlib.util.spec_from_file_location("ab2", ROOT / "scripts" / "ask-bridge.py")
        m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
        return m.ALLOWED_ROOT
    finally:
        for k in ("ASK_BRIDGE_ROOT", "REPOS_HOST_DIR"):
            os.environ.pop(k, None)
            if saved[k] is not None: os.environ[k] = saved[k]

check("root defaults to ~/Documents", root_with() == (Path.home() / "Documents").resolve())
check("root follows REPOS_HOST_DIR", root_with(REPOS_HOST_DIR="/tmp") == Path("/tmp").resolve())
check("ASK_BRIDGE_ROOT overrides", root_with(REPOS_HOST_DIR="/tmp", ASK_BRIDGE_ROOT="/usr") == Path("/usr").resolve())

# ---- pluggable agents ----
def agent_with(**env):
    """resolve_agent against an explicit env (no os.environ leakage)."""
    return bridge.resolve_agent(env)


def config_error(**env):
    try:
        bridge.resolve_agent(env)
    except bridge.AgentConfigError as e:
        return str(e)
    return None


a = agent_with()
check("default agent is claude", a.key == "claude" and a.name == "Claude" and a.binary == "claude")
c_argv, c_stdin = a.build_command("Q?", "sonnet", "claude")
check("claude preset argv unchanged", c_argv == ["claude", "-p", "--output-format", "json",
      "--allowedTools", "Read,Grep,Glob",
      "--disallowedTools", "Edit,Write,Bash,NotebookEdit,WebFetch", "--model", "sonnet"], c_argv)
check("claude preset == legacy build_command", (c_argv, c_stdin) == bridge.build_command("Q?", "sonnet", "claude"))

k = agent_with(ASK_AGENT="kiro")
k_argv, k_stdin = k.build_command("Q?", "opus", "/x/kiro-cli")
trusted = [x.split("=", 1)[1] for x in k_argv if x.startswith("--trust-tools=")]
check("kiro: single --trust-tools flag", len(trusted) == 1, k_argv)
check("kiro: only read-only tools trusted", set(trusted[0].split(",")) == {"fs_read", "grep", "glob"}, trusted)
check("kiro: never trusts all tools / write / shell", not ({"-a", "--trust-all-tools"} & set(k_argv))
      and not ({"fs_write", "execute_bash", "use_aws", "web_fetch"} & set(trusted[0].split(","))), k_argv)
check("kiro: non-interactive, prompt on stdin", "--no-interactive" in k_argv and k_stdin.startswith(bridge.READ_ONLY_PREAMBLE)
      and k_stdin.endswith("Q?") and "Q?" not in " ".join(k_argv), (k_argv, k_stdin[:60]))
check("kiro binary is kiro-cli", k.binary == "kiro-cli" and k_argv[0] == "/x/kiro-cli" and k.name == "Kiro")
check("model flag ignored for kiro (no error)", "--model" not in k_argv and "opus" not in k_argv, k_argv)

# custom command: shlex, stdin by default, {prompt} as ONE argv element, no shell
cust = agent_with(ASK_AGENT_CMD="my-agent --flag 'two words' --x=1")
argv, stdin = cust.build_command("Q; rm -rf / $(id)", "opus")
check("custom: shlex-split argv", argv == ["my-agent", "--flag", "two words", "--x=1"], argv)
check("custom: prompt on stdin with preamble", stdin.startswith(bridge.READ_ONLY_PREAMBLE) and stdin.endswith("$(id)"), stdin)
check("custom: model flag ignored", "--model" not in argv and "opus" not in argv, argv)
check("custom: name defaults to binary basename", agent_with(ASK_AGENT_CMD="/opt/bin/my-agent -x").name == "my-agent")
check("ASK_AGENT_NAME overrides display name", agent_with(ASK_AGENT_CMD="x", ASK_AGENT_NAME="Foo").name == "Foo"
      and agent_with(ASK_AGENT="kiro", ASK_AGENT_NAME="K2").name == "K2")
sub = agent_with(ASK_AGENT_CMD="my-agent --ask {prompt} --quiet")
argv, stdin = sub.build_command("multi word 'quoted' prompt", None)
check("custom {prompt}: single argv element, nothing on stdin",
      stdin is None and len(argv) == 4 and argv[2] == f"{bridge.READ_ONLY_PREAMBLE}\n\nmulti word 'quoted' prompt", argv)
check("custom: unbalanced quotes -> clear error", "shell-words" in (config_error(ASK_AGENT_CMD="a 'b") or ""))
check("ASK_AGENT_CMD overrides ASK_AGENT", agent_with(ASK_AGENT="kiro", ASK_AGENT_CMD="foo -x").key == "custom"
      and agent_with(ASK_AGENT="kiro", ASK_AGENT_CMD="foo -x").binary == "foo")
err = config_error(ASK_AGENT="nope")
check("unknown ASK_AGENT -> clear error", err and "nope" in err and "claude" in err and "kiro" in err, err)
check("ASK_AGENT is case-insensitive", agent_with(ASK_AGENT="Kiro").key == "kiro")

# never a shell: subprocess.run gets a list, shell=False, and metacharacters stay literal
calls = []
orig_run, orig_which = subprocess.run, shutil.which
def fake_run(argv, **kw):
    calls.append((argv, kw))
    return subprocess.CompletedProcess(argv, 0, stdout="ok answer\n", stderr="")
subprocess.run, shutil.which = fake_run, lambda name, *a, **k: "/bin/" + name
saved = {k_: os.environ.pop(k_, None) for k_ in ("ASK_AGENT", "ASK_AGENT_CMD", "ASK_AGENT_NAME")}
try:
    os.environ["ASK_AGENT_CMD"] = "my-agent $(touch /tmp/pwned) ; echo {prompt}"
    status, body = bridge.run_ask(str(bridge.ALLOWED_ROOT), "hi `id`", "opus")
    argv, kw = calls[-1]
    check("custom run: answer returned", status == 200 and body["answer"] == "ok answer" and body["model"] is None, (status, body))
    check("custom run: argv is a list, shell=False",
          isinstance(argv, list) and kw.get("shell") is False and kw.get("input") is None, (argv, kw))
    check("custom run: metacharacters stay literal argv elements", argv[1:4] == ["$(touch", "/tmp/pwned)", ";"], argv)
    check("custom run: metachar in prompt stays inside one element", argv[-1].endswith("hi `id`"), argv[-1])
    os.environ["ASK_AGENT_CMD"] = "my-agent"
    os.environ["ASK_AGENT_NAME"] = "Mine"
    status, body = bridge.run_ask(str(bridge.ALLOWED_ROOT), "q", "opus")
    check("custom run: stdin path, shell=False", calls[-1][1].get("shell") is False
          and calls[-1][1]["input"].startswith(bridge.READ_ONLY_PREAMBLE) and body["name"] == "Mine", calls[-1][1])
    os.environ.pop("ASK_AGENT_CMD"); os.environ.pop("ASK_AGENT_NAME")
    os.environ["ASK_AGENT"] = "kiro"
    status, body = bridge.run_ask(str(bridge.ALLOWED_ROOT), "q", "opus")
    check("kiro run: model ignored, argv is a list", status == 200 and "--model" not in calls[-1][0]
          and body["model"] is None and body["agent"] == "kiro", (status, body, calls[-1][0]))
    os.environ["ASK_AGENT"] = "bogus"
    status, body = bridge.run_ask(str(bridge.ALLOWED_ROOT), "q", None)
    check("unknown ASK_AGENT at request time -> bad_config with message", status == 500 and body["code"] == "bad_config"
          and "bogus" in body["message"], body)
finally:
    subprocess.run, shutil.which = orig_run, orig_which
    for k_ in ("ASK_AGENT", "ASK_AGENT_CMD", "ASK_AGENT_NAME"):
        os.environ.pop(k_, None)
        if saved[k_] is not None: os.environ[k_] = saved[k_]

# output parsing
ESC = "\x1b"
kiro_raw = (f"{ESC}[38;5;252m{ESC}[0m{ESC}[?25lReading file: {ESC}[38;5;141m/r/a.txt{ESC}[0m (using tool: read)\n"
            f"{ESC}[38;5;10m ✓ {ESC}[0mSuccessfully read 24 bytes\n{ESC}[38;5;244m - Completed in 0.1s{ESC}[0m\n\n"
            f"{ESC}[?25l{ESC}[38;5;141m> {ESC}[0mLine one.\nspin\rLine two\n> a quote\n{ESC}[?25h")
check("kiro parse: strips ANSI + tool trace + '> ' marker",
      bridge.parse_kiro(kiro_raw, 0) == "Line one.\nLine two\n> a quote", repr(bridge.parse_kiro(kiro_raw, 0)))
check("kiro parse: denied-tool trace is dropped", bridge.parse_kiro(
      "I will run x (using tool: shell)\n\nCommand execute_bash is rejected because it matches one or more rules on the denied list:\n"
      "  - non-interactive mode (no user to approve)\n\n> Blocked.\n", 0) == "Blocked.")
try:
    bridge.parse_kiro("", 0); ok = False
except RuntimeError:
    ok = True
check("kiro parse: empty output is an error", ok)
check("claude parse: json result", bridge.parse_claude('{"result": "hi"}', 0) == "hi")

# agent-aware not-available message + health
def which_none(name, *a, **k): return None
shutil.which = which_none
saved = {k_: os.environ.pop(k_, None) for k_ in ("ASK_AGENT", "ASK_AGENT_CMD", "ASK_AGENT_NAME")}
try:
    os.environ["ASK_AGENT"] = "kiro"
    status, body = bridge.run_ask(str(bridge.ALLOWED_ROOT), "q", None)
    check("kiro missing: message names kiro + binary", status == 503 and body["code"] == "claude_not_available"
          and body["message"] == "Ask needs the Kiro CLI (`kiro-cli`) on this machine.", body)
    h = bridge.health()
    check("health (kiro missing): agent-aware", h["available"] is False and h["agent"] == "kiro" and h["name"] == "Kiro"
          and h["claude"] is False, h)
    os.environ["ASK_AGENT_CMD"] = "/nope/fancy-agent --x"
    status, body = bridge.run_ask(str(bridge.ALLOWED_ROOT), "q", None)
    check("custom missing: message names the binary", body["message"] == "Ask needs the fancy-agent CLI (`/nope/fancy-agent`) on this machine.", body)
    os.environ.pop("ASK_AGENT_CMD"); os.environ.pop("ASK_AGENT")
    check("health (claude) keeps the legacy claude key", bridge.health()["claude"] is False and bridge.health()["agent"] == "claude")
finally:
    shutil.which = orig_which
    for k_ in ("ASK_AGENT", "ASK_AGENT_CMD", "ASK_AGENT_NAME"):
        os.environ.pop(k_, None)
        if saved[k_] is not None: os.environ[k_] = saved[k_]

# client side: status wording follows the bridge's agent
ask_client._last_agent.update({"agent": "claude", "name": "Claude", "binary": "claude"})
check("client message names agent+binary", ask_client.not_available_message({"name": "Kiro", "binary": "kiro-cli"})
      == "Ask needs the Kiro CLI (`kiro-cli`) on this machine.")
check("client not_available keeps code claude_not_available",
      ask_client.not_available({"name": "Kiro", "binary": "kiro-cli"})["code"] == "claude_not_available")

# bind address
def host_with(v):
    saved_h = os.environ.pop("ASK_BRIDGE_HOST", None)
    if v is not None: os.environ["ASK_BRIDGE_HOST"] = v
    try: return bridge.bind_host()
    finally:
        os.environ.pop("ASK_BRIDGE_HOST", None)
        if saved_h is not None: os.environ["ASK_BRIDGE_HOST"] = saved_h
check("ASK_BRIDGE_HOST defaults to 127.0.0.1", host_with(None) == "127.0.0.1")
check("ASK_BRIDGE_HOST override", host_with("172.17.0.1") == "172.17.0.1")


sys.exit(1 if fails else 0)
