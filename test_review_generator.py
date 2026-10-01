"""Tests for repo-path resolution and annotation path normalisation in review_generator.

No network, no Docker. Run:  uv run python test_review_generator.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# REPOS_DIR / REPOS_HOST_DIR must be set before the modules are imported.
_tmp = Path(tempfile.mkdtemp(prefix="srtest-")).resolve()
REPOS = _tmp / "container-repos"          # what the container sees as /repos
HOST = _tmp / "host-docs"                 # the matching host directory (never needs to exist)
(REPOS / "myrepo").mkdir(parents=True)
os.environ["REPOS_DIR"] = str(REPOS)
os.environ["REPOS_HOST_DIR"] = str(HOST)
os.environ["CLAUDE_DIR"] = str(_tmp / "claude")

sys.path.insert(0, str(Path(__file__).parent / "src"))
import review_generator as rg  # noqa: E402
from annotation_extractor import Annotation  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def resolve(cwd, cfg=None):
    try:
        return rg.resolve_repo_path(cwd, cfg)
    except Exception as e:  # report as a failed check, not a crash
        return f"raised {e!r}"


print("resolve_repo_path: direct repo path")
direct = str(HOST / "myrepo")
check("path under REPOS_HOST_DIR is returned unchanged", resolve(direct) == (direct, "myrepo"), resolve(direct))
check("empty cwd -> None", resolve("") is None)

print("resolve_repo_path: temp clone / worktree mapped by repo name")
for clone in ("/private/tmp/campaign-worktrees/x/myrepo", "/tmp/myrepo", "/var/folders/ab/T/whatever/myrepo/"):
    got = resolve(clone)
    check(f"{clone} -> host path of REPOS_DIR/myrepo", got == (str(HOST / "myrepo"), "myrepo"), got)

print("resolve_repo_path: unmappable path")
for cwd in ("/tmp/no-such-repo", "/private/tmp/long-runner/abc/myrepo-not-mounted"):
    got = resolve(cwd)
    check(f"{cwd} -> (cwd, basename) unchanged", got == (cwd, Path(cwd).name), got)

print("resolve_repo_path: repo_config precedence")
cfg = {"MyRepo": {"path": "/somewhere/else/myrepo"}}
got = resolve("/tmp/campaign/myrepo", cfg)
check("repo_config wins over the REPOS_DIR mapping", got == ("/somewhere/else/myrepo", "myrepo"), got)
got = resolve("/tmp/campaign/other", cfg)
check("repo_config without a matching name is ignored", got == ("/tmp/campaign/other", "other"), got)
got = resolve(direct, cfg)
check("direct repo path ignores repo_config (unchanged)", got == (direct, "myrepo"), got)

print("normalize_annotation_paths: session ran in an arbitrary clone")
norm = getattr(rg, "normalize_annotation_paths", None)
check("normalize_annotation_paths exists", norm is not None)


def ann(path):
    return Annotation(file_path=path, line_start=None, line_end=None, annotation_type="reasoning",
                      content="c", source_type="thinking", source_message_uuid=None)


def run(cwd, paths, repo=direct, files=("src/a.py", "pkg/b.py")):
    if norm is None:
        return None
    anns = [ann(p) for p in paths]
    try:
        norm(anns, repo, cwd, list(files))
    except Exception as e:
        return f"raised {e!r}"
    return [a.file_path for a in anns]


for cwd in ("/private/tmp/campaign-worktrees/x/myrepo", "/tmp/myrepo-ab12cd34"):
    got = run(cwd, [f"{cwd}/src/a.py", f"{direct}/pkg/b.py", "elsewhere/src/a.py", None])
    check(f"cwd {cwd}: clone-absolute, repo-absolute, suffix and None paths",
          got == ["src/a.py", "pkg/b.py", "src/a.py", None], got)
    got = run(cwd, [f"{cwd}/docs/read-only.md"])
    check(f"cwd {cwd}: file outside the diff still becomes clone-relative", got == ["docs/read-only.md"], got)

got = run(direct, [f"{direct}/src/a.py", "/unrelated/x.py"])
check("cwd == effective_repo: behaves as before", got == ["src/a.py", "/unrelated/x.py"], got)
got = run(None, [f"{direct}/src/a.py"])
check("no session cwd: repo-absolute path still normalised", got == ["src/a.py"], got)

print("path_variants: extra roots for the file->diff map")
pv = getattr(rg, "path_variants", None)
check("path_variants exists", pv is not None)
if pv:
    cwd = "/tmp/myrepo-ab12cd34"
    check("clone cwd adds a variant", set(pv(direct, cwd, "src/a.py")) == {f"{direct}/src/a.py", f"{cwd}/src/a.py"}, pv(direct, cwd, "src/a.py"))
    check("cwd == repo adds nothing", pv(direct, direct, "src/a.py") == [f"{direct}/src/a.py"])

print("generate_branch_review: session ran in a clone/worktree of the repo")
import schema  # noqa: E402
from diff_builder import DiffResult, FileDiff  # noqa: E402
from jsonl_parser import AssistantTurn, ConversationTurn, ParsedSession, ToolCall  # noqa: E402

_DIFF = (
    "@@ -1,1 +1,3 @@\n"
    " def a():\n"
    "+    compute_total_price()\n"
    "+    return 1\n"
)


def branch_review_thinking(cwd):
    diff = DiffResult(
        files=[FileDiff(file_path="src/a.py", change_type="modified", additions=2, deletions=0,
                        diff_text=_DIFF, full_content=None, language="python")],
        base_ref="main", head_ref="feat", repo_path=direct,
    )
    edit = ToolCall(tool_name="Edit", tool_id="t1", input={"file_path": f"{cwd}/src/a.py"},
                    file_path=f"{cwd}/src/a.py", timestamp=None)
    turn = ConversationTurn(
        user_text="x",
        assistant=AssistantTurn(
            uuid="u1", parent_uuid=None, timestamp=None,
            thinking_blocks=["The total must go through compute_total_price because callers expect discounts applied."],
            tool_calls=[edit],
        ),
    )
    session = ParsedSession(session_id="s1", project="myrepo", branch="feat", cwd=cwd,
                            first_timestamp=None, last_timestamp=None, turns=[turn], all_tool_calls=[edit])
    saved = (rg.build_diff, rg.find_session_jsonl, rg.parse_session_with_subagents)
    rg.build_diff = lambda repo, branch, base: diff
    rg.find_session_jsonl = lambda sid: Path(__file__)
    rg.parse_session_with_subagents = lambda path: session
    try:
        conn = schema.init_db(Path(tempfile.mkdtemp(dir=_tmp)) / "db.sqlite")
        rg.generate_branch_review(conn, direct, "feat", "main", ["s1"])
        return conn.execute(
            "SELECT f.file_path AS file_path, a.line_start AS line_start FROM annotations a "
            "LEFT JOIN review_files f ON f.id = a.file_id WHERE a.source_type = 'thinking'"
        ).fetchall()
    finally:
        rg.build_diff, rg.find_session_jsonl, rg.parse_session_with_subagents = saved


for cwd in (direct, "/private/tmp/wt/myrepo"):
    rows = [(r["file_path"], r["line_start"]) for r in branch_review_thinking(cwd)]
    check(f"cwd {cwd}: thinking note lands on src/a.py line 2", ("src/a.py", 2) in rows, rows)

# --- git worktrees: .git is a file whose gitdir is a host path the container can't see ---
import subprocess  # noqa: E402
from diff_builder import build_diff  # noqa: E402

print("\nworktree repo")


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


main_repo = REPOS / "wtrepo"
main_repo.mkdir()
git(main_repo, "init", "-q", "-b", "main")
(main_repo / "a.txt").write_text("one\n")
git(main_repo, "add", "a.txt")
git(main_repo, "commit", "-q", "-m", "base")
wt = main_repo / ".claude" / "worktrees" / "wt"
git(main_repo, "worktree", "add", "-q", "-b", "wt", str(wt))
(wt / "b.txt").write_text("two\n")
git(wt, "add", "b.txt")
git(wt, "commit", "-q", "-m", "change")
# As written by a host git: the gitdir is the host spelling of the container path.
(wt / ".git").write_text(f"gitdir: {HOST / 'wtrepo' / '.git' / 'worktrees' / 'wt'}\n")

res = build_diff(str(HOST / "wtrepo" / ".claude" / "worktrees" / "wt"), "wt", "main")
check("worktree with host-path gitdir builds a diff", res is not None and [f.file_path for f in res.files] == ["b.txt"],
      res and [f.file_path for f in res.files])
wt_host = str(HOST / "wtrepo" / ".claude" / "worktrees" / "wt")
check("worktree cwd resolves unchanged", resolve(wt_host) == (wt_host, "wt"), resolve(wt_host))
_anns = [ann(f"{wt_host}/b.txt")]
rg.normalize_annotation_paths(_anns, wt_host, wt_host, ["b.txt"])
check("worktree session paths normalise to the diff path", _anns[0].file_path == "b.txt", _anns[0].file_path)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
