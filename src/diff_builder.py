"""Generate unified diffs and capture full file content for code review.

This module runs inside a Docker container where git repos are mounted read-only
at /repos/ (host: ~/Documents/). It generates diffs between branches and captures
full file content for annotated code reviews.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pipeline_diff import build_pipeline_diff


@dataclass
class FileDiff:
    """Diff information for a single file."""

    file_path: str  # relative path within repo
    change_type: str  # "added", "modified", "deleted", "renamed", "moved"
    additions: int
    deletions: int
    diff_text: str  # unified diff text
    full_content: str | None  # full file content at HEAD for expand-around
    language: str | None  # detected from file extension
    move_metadata: str | None = None  # JSON: {"type": "moved_to"|"consolidated_from", ...}
    render_mode: str = "diff"  # "diff" (default) | "pipeline" (specialized .pp renderer)
    pipeline_diff: str | None = None  # JSON: computed semantic pipeline diff (pipeline_diff.py)


@dataclass
class DiffResult:
    """Complete diff result for a session's changes."""

    files: list[FileDiff]
    base_ref: str
    head_ref: str
    repo_path: str  # container path to repo


# Git status letter to change type mapping
_CHANGE_TYPE_MAP = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "modified",  # copied, treat as modified
    "T": "modified",  # type change, treat as modified
}

# File extension to Prism.js language mapping
_LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".scala": "scala",
    ".sc": "scala",  # mill build files
    ".sbt": "scala",
    ".mill": "scala",  # mill build files
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".sh": "bash",
    ".bash": "bash",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".pp": "json",  # Spinnaker pipeline configs are JSON (get the pipeline renderer)
    ".md": "markdown",
    ".sql": "sql",
    ".xml": "xml",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "css",
    ".less": "css",
    ".dockerfile": "docker",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".tf": "hcl",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
}


def detect_language(file_path: str) -> str | None:
    """Detect language from file extension for syntax highlighting.

    Args:
        file_path: Path to the file

    Returns:
        Prism.js language name or None if unknown
    """
    ext = Path(file_path).suffix.lower()
    return _LANGUAGE_MAP.get(ext)


def detect_render_mode(file_path: str) -> str:
    """Return a specialized renderer key for a file, or 'diff' for the default.

    ``.pp`` files are Spinnaker pipeline definitions and get the semantic
    pipeline renderer. The extension is only a hint — the actual decision is gated
    by ``build_pipeline_diff`` succeeding (it returns None for non-pipeline .pp, e.g.
    Puppet), at which point the caller falls back to 'diff'.
    """
    if Path(file_path).suffix.lower() == ".pp":
        return "pipeline"
    return "diff"


def _host_to_container(host_path: str) -> str:
    """Convert host repo path to container path.

    Args:
        host_path: Host filesystem path (e.g., /Users/foo/Documents/my-app)

    Returns:
        Container path (e.g., /repos/my-app)
    """
    repos_container_dir = os.environ.get("REPOS_DIR", "/repos")
    # Unset means host and container paths are the same (not running in the container).
    repos_host_dir = os.environ.get("REPOS_HOST_DIR") or repos_container_dir

    # Normalize paths
    host_path = os.path.normpath(host_path)
    repos_host_dir = os.path.normpath(repos_host_dir)

    # Check if host_path is under repos_host_dir
    if host_path != repos_host_dir and not host_path.startswith(repos_host_dir.rstrip("/") + "/"):
        raise ValueError(f"Host path {host_path} is not under {repos_host_dir}")

    # Get relative path and combine with container dir
    rel_path = os.path.relpath(host_path, repos_host_dir)
    return os.path.join(repos_container_dir, rel_path)


def _worktree_main_repo(repo_path: str) -> str:
    """Container path of the main repo when repo_path is a git worktree, else repo_path.

    A worktree's .git file holds a host gitdir (<main>/.git/worktrees/<name>) that
    doesn't exist in the container, so git can't run there. Branch reviews only
    diff committed refs, which the main repo shares.
    """
    git_file = os.path.join(repo_path, ".git")
    if not os.path.isfile(git_file):
        return repo_path
    try:
        with open(git_file) as f:
            first_line = f.readline().strip()
        if not first_line.startswith("gitdir:"):
            return repo_path
        git_dir = os.path.normpath(first_line[len("gitdir:"):].strip())
        main_git_dir = os.path.dirname(os.path.dirname(git_dir))
        if os.path.basename(os.path.dirname(git_dir)) != "worktrees" or os.path.basename(main_git_dir) != ".git":
            return repo_path
        return _host_to_container(os.path.dirname(main_git_dir))
    except (OSError, ValueError):
        return repo_path


def _parse_diff_stat(diff_text: str) -> tuple[int, int]:
    """Count additions and deletions from unified diff text.

    Args:
        diff_text: Unified diff output

    Returns:
        Tuple of (additions, deletions)
    """
    additions = 0
    deletions = 0

    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

    return additions, deletions


def _run_git_command(
    repo_path: str,
    args: list[str],
    timeout: int = 30,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a git command in the specified repository.

    Args:
        repo_path: Path to the git repository
        args: Git command arguments (without 'git')
        timeout: Command timeout in seconds
        check: Whether to raise on non-zero exit

    Returns:
        CompletedProcess with captured output
    """
    cmd = ["git", "-C", repo_path] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _get_merge_base(repo_path: str, base_ref: str, head_ref: str) -> str | None:
    """Find the merge base between two refs.

    Args:
        repo_path: Path to the git repository
        base_ref: Base reference
        head_ref: Head reference

    Returns:
        Merge base commit SHA or None if not found
    """
    try:
        result = _run_git_command(
            repo_path,
            ["merge-base", base_ref, head_ref],
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _get_changed_files(
    repo_path: str,
    base_ref: str,
    head_ref: str,
) -> list[tuple[str, str]]:
    """Get list of changed files with their change types.

    Args:
        repo_path: Path to the git repository
        base_ref: Base reference
        head_ref: Head reference

    Returns:
        List of (change_type_letter, file_path) tuples
    """
    try:
        result = _run_git_command(
            repo_path,
            ["diff", f"{base_ref}...{head_ref}", "--name-status"],
        )

        files = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue

            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue

            status, file_path = parts
            # Handle rename with similarity score (e.g., "R100")
            status_letter = status[0]

            # For renames, we get "old_path\tnew_path"
            if status_letter == "R":
                file_path = file_path.split("\t")[-1]  # Use new path

            files.append((status_letter, file_path))

        return files
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []


def _get_file_diff(
    repo_path: str,
    base_ref: str,
    head_ref: str,
    file_path: str,
) -> str:
    """Get unified diff for a specific file.

    Args:
        repo_path: Path to the git repository
        base_ref: Base reference
        head_ref: Head reference
        file_path: Relative path to the file within the repo

    Returns:
        Unified diff text
    """
    try:
        result = _run_git_command(
            repo_path,
            ["diff", f"{base_ref}...{head_ref}", "--", file_path],
        )
        return result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def _get_file_content(
    repo_path: str,
    ref: str,
    file_path: str,
) -> str | None:
    """Get full content of a file at a specific ref.

    Args:
        repo_path: Path to the git repository
        ref: Git reference (branch, commit, etc.)
        file_path: Relative path to the file within the repo

    Returns:
        File content or None if file doesn't exist at ref
    """
    try:
        result = _run_git_command(
            repo_path,
            ["show", f"{ref}:{file_path}"],
        )
        return result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _detect_moves(
    file_diffs: list[FileDiff],
    repo_path: str,
    base_ref: str,
    head_ref: str,
    containment_threshold: float = 0.40,
) -> None:
    """Detect moved/consolidated files and replace their diffs with cross-file diffs.

    For each deleted file, compare its original content against added AND modified
    files using containment ratio (what % of the source appears in the target).
    This works better than symmetric similarity for detecting merges where a small
    file is absorbed into a larger one.

    Mutates file_diffs in place.
    """
    deleted = [f for f in file_diffs if f.change_type == "deleted"]
    # Potential targets: added files (pure move) OR modified files (merged into existing)
    candidates = [f for f in file_diffs if f.change_type in ("added", "modified")]

    if not deleted or not candidates:
        return

    # Pre-fetch content for deleted files (from base ref)
    deleted_content: dict[str, list[str]] = {}
    for fd in deleted:
        content = _get_file_content(repo_path, base_ref, fd.file_path)
        if content:
            deleted_content[fd.file_path] = content.splitlines(keepends=True)

    # Pre-fetch content for candidate target files (from head ref)
    candidate_content: dict[str, list[str]] = {}
    for fd in candidates:
        content = _get_file_content(repo_path, head_ref, fd.file_path)
        if content:
            candidate_content[fd.file_path] = content.splitlines(keepends=True)

    # Track which target files have been matched
    matched_targets: dict[str, list[dict]] = {}  # target_path -> list of source info

    for del_fd in deleted:
        if del_fd.file_path not in deleted_content:
            continue
        del_lines = deleted_content[del_fd.file_path]
        del_ext = Path(del_fd.file_path).suffix.lower()

        best_match = None
        best_containment = 0.0

        for cand_fd in candidates:
            if cand_fd.file_path not in candidate_content:
                continue
            # Skip if file extensions differ
            cand_ext = Path(cand_fd.file_path).suffix.lower()
            if del_ext != cand_ext:
                continue

            cand_lines = candidate_content[cand_fd.file_path]

            # For large files, do a quick pre-check with basename overlap
            if len(del_lines) > 2000 or len(cand_lines) > 2000:
                del_base = Path(del_fd.file_path).stem.lower()
                cand_base = Path(cand_fd.file_path).stem.lower()
                if del_base not in cand_base and cand_base not in del_base:
                    continue

            matcher = difflib.SequenceMatcher(None, del_lines, cand_lines, autojunk=False)
            # Containment: what fraction of the deleted file's lines appear in the target
            # This catches merges where a small file is absorbed into a larger one
            matched_source_lines = sum(b.size for b in matcher.get_matching_blocks())
            containment = matched_source_lines / len(del_lines) if del_lines else 0

            if containment > best_containment:
                best_containment = containment
                best_match = cand_fd

        if best_match and best_containment >= containment_threshold:
            cand_lines = candidate_content[best_match.file_path]
            containment_pct = round(best_containment * 100)

            # Generate cross-file unified diff (deleted source → target at HEAD)
            cross_diff = "".join(difflib.unified_diff(
                del_lines,
                cand_lines,
                fromfile=f"a/{del_fd.file_path}",
                tofile=f"b/{best_match.file_path}",
            ))

            # Replace the deleted file's diff with the cross-file diff
            del_fd.diff_text = cross_diff
            del_fd.change_type = "moved"
            # Set full_content to the target file's content (enables expand-around)
            new_content = _get_file_content(repo_path, head_ref, best_match.file_path)
            del_fd.full_content = new_content
            # Recount additions/deletions from the cross-file diff
            del_fd.additions, del_fd.deletions = _parse_diff_stat(cross_diff)
            del_fd.move_metadata = json.dumps({
                "type": "moved_to",
                "target": best_match.file_path,
                "similarity": containment_pct,
            })

            # Track this match for the target file
            if best_match.file_path not in matched_targets:
                matched_targets[best_match.file_path] = []
            matched_targets[best_match.file_path].append({
                "source": del_fd.file_path,
                "similarity": containment_pct,
            })

    # Process target files: compute section line ranges and generate composite diffs
    for cand_fd in candidates:
        if cand_fd.file_path not in matched_targets:
            continue

        sources_for_target = matched_targets[cand_fd.file_path]
        tgt_lines = candidate_content.get(cand_fd.file_path)
        if not tgt_lines:
            continue

        # Compute where each source's content maps to in the target
        sections = []
        for src_info in sources_for_target:
            src_path = src_info["source"]
            src_lines = deleted_content.get(src_path)
            if not src_lines:
                continue

            matcher = difflib.SequenceMatcher(None, src_lines, tgt_lines, autojunk=False)
            blocks = [b for b in matcher.get_matching_blocks() if b.size > 0]

            if blocks:
                tgt_start = min(b.b for b in blocks)
                tgt_end = max(b.b + b.size for b in blocks)
                sections.append({
                    "source": src_path,
                    "target_start": tgt_start + 1,  # 1-indexed for frontend
                    "target_end": tgt_end,
                    "similarity": src_info["similarity"],
                })

        sections.sort(key=lambda s: s["target_start"])

        # Generate composite diff for target files (both added and modified).
        # The "old" side is: base content of the target (if it existed) + all source
        # file contents. The "new" side is the target at HEAD.
        # This makes moved-unchanged lines appear as context (gray) instead of
        # additions (green), so only actual modifications show as red/green.
        base_content = _get_file_content(repo_path, base_ref, cand_fd.file_path)
        base_lines = base_content.splitlines(keepends=True) if base_content else []

        # Collect all source file contents
        all_source_lines = []
        for src_info in sources_for_target:
            src_lines = deleted_content.get(src_info["source"], [])
            all_source_lines.extend(src_lines)

        composite_old = base_lines + all_source_lines
        if composite_old:
            from_label = cand_fd.file_path
            if all_source_lines:
                src_names = " + ".join(s["source"] for s in sources_for_target)
                from_label = f"{cand_fd.file_path} + {src_names}"
            cross_diff = "".join(difflib.unified_diff(
                composite_old,
                tgt_lines,
                fromfile=f"a/{from_label}",
                tofile=f"b/{cand_fd.file_path}",
            ))
            if cross_diff:
                cand_fd.diff_text = cross_diff
                cand_fd.additions, cand_fd.deletions = _parse_diff_stat(cross_diff)

        cand_fd.move_metadata = json.dumps({
            "type": "consolidated_from",
            "sources": sources_for_target,
            "sections": sections,
        })


def build_diff(
    repo_host_path: str,
    branch: str,
    base_ref: str = "main",
) -> DiffResult | None:
    """Generate diffs for all changed files between base_ref and branch.

    Args:
        repo_host_path: Host path to the repo (e.g., /Users/foo/Documents/my-app)
        branch: The branch with changes
        base_ref: The base to diff against (default: "main")

    Returns:
        DiffResult or None if repo/branch not accessible
    """
    try:
        # Convert host path to container path
        repo_path = _worktree_main_repo(_host_to_container(repo_host_path))

        # Verify repo exists
        if not os.path.isdir(repo_path):
            return None

        # Verify it's a git repo
        try:
            _run_git_command(repo_path, ["rev-parse", "--git-dir"])
        except subprocess.CalledProcessError:
            return None

        # Verify both refs exist
        try:
            _run_git_command(repo_path, ["rev-parse", "--verify", base_ref])
            _run_git_command(repo_path, ["rev-parse", "--verify", branch])
        except subprocess.CalledProcessError:
            return None

        # Get changed files
        changed_files = _get_changed_files(repo_path, base_ref, branch)
        if not changed_files:
            # No changes, return empty result
            return DiffResult(
                files=[],
                base_ref=base_ref,
                head_ref=branch,
                repo_path=repo_path,
            )

        # Build FileDiff for each changed file
        file_diffs = []
        for status_letter, file_path in changed_files:
            change_type = _CHANGE_TYPE_MAP.get(status_letter, "modified")

            # Get diff text
            diff_text = _get_file_diff(repo_path, base_ref, branch, file_path)

            # Parse stats
            additions, deletions = _parse_diff_stat(diff_text)

            # Get full content at head (skip for deleted files)
            full_content = None
            if change_type != "deleted":
                full_content = _get_file_content(repo_path, branch, file_path)

            # Detect language
            language = detect_language(file_path)

            # Specialized renderer routing (.pp pipelines). Compute the semantic
            # diff here — this loop runs container-side with git access to BOTH
            # refs, so no new fetch path is needed. Any parse failure or a
            # non-pipeline .pp yields None and we fall back to the raw line diff,
            # which is always available.
            render_mode = detect_render_mode(file_path)
            pipeline_diff_json: str | None = None
            if render_mode == "pipeline":
                try:
                    old_content = (
                        None if change_type == "added"
                        else _get_file_content(repo_path, base_ref, file_path)
                    )
                    payload = build_pipeline_diff(old_content, full_content)
                    if payload is not None:
                        pipeline_diff_json = json.dumps(payload)
                    else:
                        render_mode = "diff"  # not a Spinnaker pipeline → raw diff
                except Exception:
                    render_mode = "diff"  # never let pipeline analysis break a review

            file_diffs.append(
                FileDiff(
                    file_path=file_path,
                    change_type=change_type,
                    additions=additions,
                    deletions=deletions,
                    diff_text=diff_text,
                    full_content=full_content,
                    language=language,
                    render_mode=render_mode,
                    pipeline_diff=pipeline_diff_json,
                )
            )

        # Detect cross-file moves/consolidations
        _detect_moves(file_diffs, repo_path, base_ref, branch)

        return DiffResult(
            files=file_diffs,
            base_ref=base_ref,
            head_ref=branch,
            repo_path=repo_path,
        )

    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None
