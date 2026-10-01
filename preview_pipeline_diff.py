#!/usr/bin/env python3
"""Render a .pp semantic diff standalone, for eyeballing and for the render tests.

The review server needs a review row, a session and a browser; iterating on the
renderer through it is slow. This builds the same payload from two git blobs and
drops it into a page that loads the real ``style.css`` and ``pipeline-diff.js``,
so what you see is what the server serves.

    # two .pp files (use /dev/null for an absent side)
    ./preview_pipeline_diff.py tests/fixtures/pipelines/shell-edit.old.pp \\
        tests/fixtures/pipelines/shell-edit.new.pp --open

    # a real commit in a git repo
    ./preview_pipeline_diff.py --git ~/Documents/my-app <commit> pipelines/deploy.pp --open

    # just the payload, for test_pipeline_render.js
    ./preview_pipeline_diff.py <old.pp> <new.pp> --json /tmp/payload.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from pipeline_diff import build_pipeline_diff  # noqa: E402


def show(repo: Path, ref: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>pipeline diff preview — {title}</title>
<link rel="stylesheet" href="file://{css}">
<style>body {{ margin: 0; }} .preview-head {{ padding: 10px 14px; border-bottom: 1px solid var(--border);
  font: 13px ui-monospace, Menlo, monospace; color: var(--text-muted); }}</style>
</head><body>
<div class="preview-head">{title}</div>
<div class="pipeline-view" id="root"></div>
<script src="file://{js}"></script>
<script>
  var payload = {payload};
  var root = document.getElementById('root');
  root.innerHTML = window.PipelineDiff.render({{ pipeline_diff: JSON.stringify(payload) }});
  window.PipelineDiff.wire(root);
</script>
</body></html>
"""


def read_file(path: Path) -> str | None:
    text = path.read_text() if path.exists() else ""
    return text or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("old_or_repo", type=Path, help="old .pp file, or the repo with --git")
    ap.add_argument("new_or_commit", help="new .pp file, or the commit with --git")
    ap.add_argument("path", nargs="?", help="with --git: path to the .pp inside the repo")
    ap.add_argument("--git", action="store_true",
                    help="read the two sides from git: <repo> <commit> <path>, diffing commit~1..commit")
    ap.add_argument("--base", help="with --git: diff against this ref instead of commit~1 "
                                   "(for branch reviews, whose base is not the parent)")
    ap.add_argument("--html", type=Path, help="write the preview page here")
    ap.add_argument("--json", type=Path, help="write the raw payload here")
    ap.add_argument("--open", action="store_true", help="open the preview in a browser")
    args = ap.parse_args()

    if args.git:
        if not args.path:
            ap.error("--git needs <repo> <commit> <path>")
        repo, commit = args.old_or_repo, args.new_or_commit
        old = show(repo, args.base or f"{commit}~1", args.path)
        new = show(repo, commit, args.path)
        title = f"{args.path} @ {commit}  ({repo.name})"
    else:
        old = read_file(args.old_or_repo)
        new = read_file(Path(args.new_or_commit))
        title = f"{args.old_or_repo.name} → {Path(args.new_or_commit).name}"
    if old is None and new is None:
        print("neither side exists", file=sys.stderr)
        return 1

    payload = build_pipeline_diff(old, new)
    if payload is None:
        print("input does not parse as a pipeline — the server would fall back "
              "to the raw diff here", file=sys.stderr)
        return 1

    if args.json:
        args.json.write_text(json.dumps(payload, ensure_ascii=False))
        print(f"payload → {args.json}")

    html_path = args.html or Path("/tmp/pp-preview.html")
    html_path.write_text(PAGE.format(
        title=title,
        css=ROOT / "static" / "css" / "style.css",
        js=ROOT / "static" / "js" / "pipeline-diff.js",
        payload=json.dumps(payload, ensure_ascii=False),
    ))
    print(f"preview → {html_path}")
    if args.open:
        subprocess.run(["open", str(html_path)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
