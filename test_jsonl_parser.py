"""Tests for jsonl_parser.parse_session: every assistant record in a session must reach a turn.

Claude Code writes one prompt as many records: assistant text and tool_use blocks as separate
"assistant" records, and each tool result as a "user" record with only tool_result content.

No network, no Docker. Run:  uv run python test_jsonl_parser.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from jsonl_parser import parse_session  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def user(text, n):
    return {"type": "user", "uuid": f"u{n}", "sessionId": "s1", "cwd": "/repos/app", "gitBranch": "feat",
            "timestamp": f"2026-01-01T00:00:{n:02d}Z", "message": {"role": "user", "content": text}}


def tool_result(tool_id, n):
    return {"type": "user", "uuid": f"r{n}", "sessionId": "s1", "timestamp": f"2026-01-01T00:00:{n:02d}Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


def assistant(blocks, n):
    return {"type": "assistant", "uuid": f"a{n}", "sessionId": "s1", "timestamp": f"2026-01-01T00:00:{n:02d}Z",
            "message": {"role": "assistant", "content": blocks}}


def text(t):
    return {"type": "text", "text": t}


def edit(tool_id, path):
    return {"type": "tool_use", "id": tool_id, "name": "Edit",
            "input": {"file_path": path, "old_string": "a", "new_string": "b"}}


def parse(records):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(json.dumps(r) for r in records) + "\n")
    return parse_session(Path(f.name))


PROMPT = "Add a --tag filter to the list command."

print("one prompt, several tool calls (Claude Code's record layout)")
s = parse([
    user(PROMPT, 1),
    assistant([text("Let me look at the CLI first.")], 2),
    assistant([edit("t1", "/repos/app/cli.py")], 3),
    tool_result("t1", 4),
    assistant([text("Now the store needs to filter by tag, so notes match any requested tag.")], 5),
    assistant([edit("t2", "/repos/app/store.py")], 6),
    tool_result("t2", 7),
    assistant([text("Done: --tag is repeatable and documented in docs/configuration.md.")], 8),
])
texts = [t for turn in s.turns for t in turn.assistant.text_blocks]
edits = [c.file_path for turn in s.turns for c in turn.assistant.tool_calls]
check("text before the first tool call is kept", "Let me look at the CLI first." in texts, texts)
check("text after a tool result is kept", any(t.startswith("Now the store") for t in texts), texts)
check("final summary is kept", any(t.startswith("Done:") for t in texts), texts)
check("every edit reaches a turn", edits == ["/repos/app/cli.py", "/repos/app/store.py"], edits)
check("turns keep the real prompt, not tool-result text", all(t.user_text == PROMPT for t in s.turns),
      [t.user_text for t in s.turns])
check("all_tool_calls still lists both edits", [c.file_path for c in s.all_tool_calls] == edits)

print("two prompts")
s = parse([
    user("first prompt", 1),
    assistant([text("answer one")], 2),
    user("second prompt", 3),
    assistant([edit("t1", "/repos/app/a.py")], 4),
    tool_result("t1", 5),
    assistant([text("answer two")], 6),
])
pairs = [(t.user_text, t.assistant.text_blocks + [c.file_path for c in t.assistant.tool_calls]) for t in s.turns]
check("each assistant record is paired with the prompt it answers",
      [u for u, items in pairs for _ in items] == ["first prompt", "second prompt", "second prompt"], pairs)
check("session metadata comes from the first user record", (s.session_id, s.cwd, s.branch) == ("s1", "/repos/app", "feat"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
