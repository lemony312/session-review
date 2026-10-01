"""Annotation placement must not depend on PYTHONHASHSEED.

No network, no Docker. Run:  uv run python test_annotation_determinism.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).parent / "src")

SNIPPET = f"""
import json, sys
sys.path.insert(0, {SRC!r})
from jsonl_parser import AssistantTurn, ConversationTurn, ParsedSession
from annotation_extractor import extract_annotations

def diff(path, body):
    return f"--- a/{{path}}\\n+++ b/{{path}}\\n@@ -1,1 +1,3 @@\\n line1\\n+{{body}}\\n+other\\n"

file_diffs = {{
    "src/a.py": diff("src/a.py", "def compute_total_price(x): return round(x, 2)"),
    "src/data.py": diff("src/data.py", "DATA = 1"),
    "src/util.py": diff("src/util.py", "def helper(): pass"),
}}
thinking = "Looking at a.py and util.py: compute_total_price rounds the value before returning it to callers, which changes how every downstream invoice total is computed and displayed."
assistant = AssistantTurn(uuid="u1", parent_uuid=None, timestamp=None, thinking_blocks=[thinking])
session = ParsedSession(
    session_id="s", project=None, branch=None, cwd=None,
    first_timestamp=None, last_timestamp=None,
    turns=[ConversationTurn(user_text="short", assistant=assistant)],
)
out = [(a.file_path, a.line_start, a.line_end, a.sort_order, a.content[:40])
       for a in extract_annotations(session, file_diffs)]
print(json.dumps(out))
"""

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def run(seed):
    r = subprocess.run([sys.executable, "-c", SNIPPET], capture_output=True, text=True,
                       env={**os.environ, "PYTHONHASHSEED": str(seed)})
    return r.stdout.strip() if r.returncode == 0 else f"error: {r.stderr[-300:]}"


print("extract_annotations: same output for PYTHONHASHSEED 0..7")
outs = {s: run(s) for s in range(8)}
check("all 8 outputs identical", len(set(outs.values())) == 1,
      "\n" + "\n".join(f"    seed {s}: {o}" for s, o in outs.items()))

first = outs[0]
try:
    anns = json.loads(first)
except ValueError:
    anns = []
note = [a for a in anns if a[4].startswith("Looking at a.py")]
check("thinking note lands on src/a.py", bool(note) and note[0][0] == "src/a.py", first)
check("thinking note maps to new line 2", bool(note) and note[0][1] == 2, first)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
