"""
End-to-end check: a rendered-markdown file view opens at its first change, not the top.

Uses a real browser (Playwright) against a running session-review server, but feeds the
page a synthetic review through a mocked /api/reviews/<id>, so no git repo is needed.
Fixture: a 300-line README whose only edit is near the bottom, plus a short one.

    uv run --with playwright python test_scroll_to_change.py [BASE_URL]   # default http://localhost:8087
    (first time: uv run --with playwright playwright install chromium)
"""
import json
import sys

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8087").rstrip("/")
N = 300
EDIT = 240  # 1-based line of the edit


def long_doc(edited):
    lines = []
    for i in range(1, N + 1):
        lines.append(f"Paragraph {i} " + ("edited text here" if edited and i == EDIT else "unchanged text here"))
        lines.append("")
    return "\n".join(lines)


def diff_for(old, new):
    import difflib
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        "a/README.md", "b/README.md", n=3))
    return "diff --git a/README.md b/README.md\n" + body


old, new = long_doc(False), long_doc(True)
FILE = {"id": 1, "file_path": "README.md", "change_type": "modified", "additions": 1, "deletions": 1,
        "diff_text": diff_for(old, new), "full_content": new, "language": "markdown",
        "move_metadata": None, "render_mode": "diff", "pipeline_diff": None}
REVIEW = {"review": {"id": 999, "project": "p", "branch": "b", "repo_path": "/x", "base_ref": "main",
                     "title": "t", "summary": "s", "status": "draft", "total_files_changed": 1,
                     "total_additions": 1, "total_deletions": 1, "session_date": "2026-01-01 00:00:00",
                     "generated_at": "2026-01-01 00:00:00", "session_id": None, "source_jsonl": None},
          "files": [FILE], "annotations": []}

VISIBLE_JS = """() => {
  const c = document.querySelector('.markdown-rendered-container');
  const t = c.querySelector('.md-added, .md-removed, .md-modified');
  if (!t) return {err: 'no changed block'};
  const cr = c.getBoundingClientRect(), tr = t.getBoundingClientRect();
  return {scrollTop: c.scrollTop, scrollable: c.scrollHeight > c.clientHeight + 1,
          gap: tr.top - cr.top, visible: tr.top >= cr.top - 1 && tr.bottom <= cr.bottom + 1,
          nearTop: tr.top - cr.top < 120};
}"""

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if not cond else ""))
    fails += 0 if cond else 1


with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_context(viewport={"width": 1400, "height": 900}).new_page()
    page.route("**/api/reviews/999", lambda r: r.fulfill(json=REVIEW))
    page.goto(f"{BASE}/review/999")
    page.wait_for_selector(".markdown-rendered-container")
    page.wait_for_timeout(300)

    print("first render of a long markdown file")
    s = page.evaluate(VISIBLE_JS)
    check("container is scrollable (fixture is long enough)", s.get("scrollable"), s)
    check("first changed block is visible in the container", s.get("visible"), s)
    check("and sits near the top with a little context above", s.get("nearTop") and s.get("gap", 0) > 0, s)

    print("toggling the raw diff and back")
    page.click(".markdown-toggle-btn")
    page.click(".markdown-toggle-btn")
    s = page.evaluate(VISIBLE_JS)
    check("still at the first change after View Diff / View Rendered", s.get("visible") and s.get("nearTop"), s)

    print("user scroll is respected")
    page.evaluate("document.querySelector('.markdown-rendered-container').scrollTop = 50")
    page.click(".markdown-toggle-btn")
    page.click(".markdown-toggle-btn")
    top = page.evaluate("document.querySelector('.markdown-rendered-container').scrollTop")
    check("scroll position kept across toggle after the user scrolled", abs(top - 50) < 2, top)
    page.click(".diff-mode-seg-global [data-mode=split]")
    page.wait_for_timeout(200)
    top2 = page.evaluate("document.querySelector('.markdown-rendered-container').scrollTop")
    check("switching diff mode does not move the rendered view", abs(top2 - 50) < 2, top2)
    b.close()

print(f"\n{'FAILED' if fails else 'passed'} ({fails} failures)")
sys.exit(1 if fails else 0)
