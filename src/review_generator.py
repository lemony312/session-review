"""Orchestrate the full review generation pipeline.

Pipeline: locate JSONL → parse → diff → extract annotations → persist.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from collections import defaultdict

from jsonl_parser import parse_session, parse_session_with_subagents, parse_session_metadata, get_file_edits, ParsedSession
from diff_builder import build_diff, DiffResult, FileDiff, _host_to_container
from ask_client import to_host_path
from annotation_extractor import extract_annotations, merge_annotations, Annotation

CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", "/claude"))
CLAUDE_HOST_DIR = os.environ.get("CLAUDE_HOST_DIR", str(Path.home() / ".claude"))


def find_session_jsonl(session_id: str) -> Path | None:
    """Locate the JSONL file for a given session ID.

    Searches through all project directories in the Claude config.
    """
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.exists():
        return None

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        # Main session files
        for jsonl in project_dir.glob("*.jsonl"):
            if session_id in jsonl.stem:
                return jsonl
        # Subagent files
        for jsonl in project_dir.glob("*/subagents/*.jsonl"):
            if session_id in jsonl.stem:
                return jsonl

    return None


def _extract_project_name(path: Path) -> str:
    """Extract human-readable project name from Claude project dir path."""
    # Claude stores projects as: ~/.claude/projects/-Users-name-Documents-reponame/
    parts = path.parent.name.strip("-").split("-")
    return parts[-1] if parts else path.parent.name


def _container_to_host_path(container_path: str) -> str:
    """Convert container path back to host path."""
    if container_path.startswith(str(CLAUDE_DIR)):
        return container_path.replace(str(CLAUDE_DIR), CLAUDE_HOST_DIR, 1)
    return container_path


def _generate_summary(session: ParsedSession, diff_result: DiffResult | None) -> str:
    """Generate a review summary from session data."""
    parts = []

    if session.turns:
        # Use first user prompt as context
        first_prompt = session.turns[0].user_text
        if first_prompt and len(first_prompt) > 30:
            parts.append(f"**Goal:** {first_prompt[:500]}")

    if diff_result and diff_result.files:
        stats = f"{len(diff_result.files)} files changed"
        total_add = sum(f.additions for f in diff_result.files)
        total_del = sum(f.deletions for f in diff_result.files)
        stats += f", +{total_add} -{total_del}"
        parts.append(stats)

    # Last assistant text as conclusion
    if session.turns:
        last_text = session.turns[-1].assistant.text_blocks
        if last_text:
            conclusion = last_text[-1][:500]
            parts.append(f"**Conclusion:** {conclusion}")

    return "\n\n".join(parts) if parts else "No summary available"


def _generate_title(session: ParsedSession) -> str:
    """Generate a review title from session data."""
    if session.turns:
        first_prompt = session.turns[0].user_text
        if first_prompt:
            # Take first sentence or first 80 chars
            title = first_prompt.split("\n")[0][:80]
            if len(first_prompt.split("\n")[0]) > 80:
                title += "..."
            return title
    return f"Session {session.session_id[:8]}"


def resolve_repo_path(cwd: str, repo_config: dict | None = None) -> tuple[str, str] | None:
    """Resolve a session working directory to (host_repo_path, repo_name).

    A cwd inside REPOS_HOST_DIR is a repo the container can see and is returned
    as is. Any other cwd (a temp clone, a worktree elsewhere) is mapped by its
    last path component: repo_config first, then REPOS_DIR/<name> translated to
    its host path. If neither matches, (cwd, name) is returned unchanged.

    Returns:
        (repo_path, repo_name) or None if cwd is empty.
    """
    if not cwd:
        return None

    repo_name = Path(cwd.rstrip("/")).name
    if not repo_name:
        return None

    try:
        _host_to_container(cwd)
        return (cwd, repo_name)
    except ValueError:
        pass

    if repo_config:
        for key, cfg in repo_config.items():
            if key.lower() == repo_name.lower():
                return (cfg.get("path", cwd), repo_name)

    mounted = os.path.join(os.environ.get("REPOS_DIR", "/repos"), repo_name)
    if os.path.isdir(mounted):
        return (to_host_path(mounted), repo_name)

    return (cwd, repo_name)


def path_variants(effective_repo: str, session_cwd: str | None, rel_path: str) -> list[str]:
    """Absolute spellings of a diff file: under the repo, and under the session's own clone."""
    variants = [os.path.join(effective_repo, rel_path)]
    if session_cwd and session_cwd != effective_repo:
        variants.append(os.path.join(session_cwd, rel_path))
    return variants


def normalize_annotation_paths(
    annotations: list[Annotation],
    effective_repo: str | None,
    session_cwd: str | None,
    diff_files: list[str],
) -> None:
    """Rewrite annotation file paths to the repo-relative paths used by the diff (in place).

    JSONL tool calls carry absolute paths from wherever the session ran (the repo,
    or any clone/worktree of it); the diff uses paths relative to the repo.
    """
    roots = [r for r in (effective_repo, session_cwd) if r]
    known = {v: rel for rel in diff_files for v in path_variants(effective_repo, session_cwd, rel)} if effective_repo else {}
    for ann in annotations:
        if not ann.file_path:
            continue
        if ann.file_path in known:
            ann.file_path = known[ann.file_path]
            continue
        for root in roots:
            if ann.file_path.startswith(root):
                ann.file_path = os.path.relpath(ann.file_path, root)
                break
        else:
            # Not under either root (e.g. read for context): match by suffix.
            for rel in diff_files:
                if ann.file_path.endswith(rel):
                    ann.file_path = rel
                    break


def list_active_branches(
    repo_config: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Discover active branches from session JSONL files grouped by repo:branch.

    Scans all JSONL files, extracts lightweight metadata, and groups
    sessions by (repo_path, branch). Optionally cross-references with
    existing reviews in the DB.
    """
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.exists():
        return []

    # Collect session metadata
    sessions_by_branch: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            meta = parse_session_metadata(jsonl)
            if not meta or not meta.get("branch"):
                continue

            resolved = resolve_repo_path(meta["cwd"] or "", repo_config)
            if not resolved:
                continue
            repo_path, repo_name = resolved

            key = (repo_path, meta["branch"])
            sessions_by_branch[key].append({
                "session_id": meta["session_id"],
                "first_prompt": meta.get("first_prompt", ""),
                "mtime": jsonl.stat().st_mtime,
                "source_file": str(jsonl),
            })

    # Build branch list
    branches = []
    for (repo_path, branch), sessions in sessions_by_branch.items():
        sessions.sort(key=lambda s: s["mtime"], reverse=True)
        repo_name = Path(repo_path).name

        entry = {
            "repo_path": repo_path,
            "repo_name": repo_name,
            "branch": branch,
            "key": f"{repo_name}:{branch}",
            "session_count": len(sessions),
            "sessions": sessions,
            "latest_mtime": sessions[0]["mtime"],
            "has_review": False,
            "review_id": None,
        }

        # Check for existing review
        if conn:
            existing = conn.execute(
                "SELECT id FROM reviews WHERE repo_path = ? AND branch = ?",
                (repo_path, branch),
            ).fetchone()
            if existing:
                entry["has_review"] = True
                entry["review_id"] = existing["id"]

        branches.append(entry)

    branches.sort(key=lambda b: b["latest_mtime"], reverse=True)
    return branches


def _compute_diff_hash(diff_result: DiffResult) -> str:
    """Compute a stable hash of the diff for staleness detection.

    Args:
        diff_result: The diff result to hash

    Returns:
        SHA256 hash of all file diffs concatenated
    """
    hasher = hashlib.sha256()
    for file_diff in sorted(diff_result.files, key=lambda f: f.file_path):
        # Include file path and diff text for complete signature
        hasher.update(file_diff.file_path.encode('utf-8'))
        hasher.update(b'\x00')  # separator
        hasher.update(file_diff.diff_text.encode('utf-8'))
        hasher.update(b'\x00')
    return hasher.hexdigest()


def generate_review(
    conn: sqlite3.Connection,
    session_id: str,
    jsonl_path: Path | None = None,
    repo_path: str | None = None,
    branch: str | None = None,
    base_ref: str = "main",
) -> tuple[int, str]:
    """Generate a full review for a session and persist it.

    Uses upsert logic keyed on (repo_path, branch, base_ref). If a review already
    exists with the same diff hash, returns the existing review_id without regenerating.

    Args:
        conn: SQLite connection
        session_id: Claude session UUID
        jsonl_path: Optional direct path to JSONL file
        repo_path: Optional host repo path override
        branch: Optional branch override
        base_ref: Base ref to diff against

    Returns:
        Tuple of (review_id, status) where status is "unchanged" or "generated"

    Raises:
        FileNotFoundError: If JSONL file can't be found
        ValueError: If session has no meaningful content
    """
    # Step 1: Locate JSONL
    if jsonl_path is None:
        jsonl_path = find_session_jsonl(session_id)
    if jsonl_path is None or not jsonl_path.exists():
        raise FileNotFoundError(f"Cannot find JSONL for session {session_id}")

    # Step 2: Parse session (with subagents)
    session = parse_session_with_subagents(jsonl_path)
    if not session.turns:
        raise ValueError(f"Session {session_id} has no conversation turns")

    # Use parsed metadata, with overrides
    project = session.project or _extract_project_name(jsonl_path)
    effective_branch = branch or session.branch
    effective_repo = repo_path

    # Try to resolve repo path from session cwd
    if not effective_repo and session.cwd:
        effective_repo = session.cwd

    # A cwd the container can't see (temp clone, other worktree) maps to the mounted repo of the same name
    if effective_repo:
        resolved = resolve_repo_path(effective_repo)
        if resolved:
            effective_repo = resolved[0]

    # Step 3: Build diffs
    diff_result: DiffResult | None = None
    diff_hash: str | None = None
    if effective_repo and effective_branch:
        diff_result = build_diff(effective_repo, effective_branch, base_ref)
        if diff_result:
            diff_hash = _compute_diff_hash(diff_result)

    # Step 3.5: Check for existing review with same diff
    if effective_repo and effective_branch and diff_hash:
        existing = conn.execute(
            "SELECT id, diff_hash, session_id FROM reviews WHERE repo_path = ? AND branch = ? AND base_ref = ?",
            (effective_repo, effective_branch, base_ref),
        ).fetchone()

        if existing and existing["diff_hash"] == diff_hash:
            # Diff unchanged — update session_id association if needed and return
            if existing["session_id"] != session_id:
                conn.execute(
                    "UPDATE reviews SET session_id = ? WHERE id = ?",
                    (session_id, existing["id"]),
                )
                conn.commit()
            return (existing["id"], "unchanged")

    # Step 4: Extract annotations
    # JSONL tool calls use absolute paths, git diff uses relative paths
    file_diffs_map: dict[str, str] = {}
    if diff_result:
        for f in diff_result.files:
            file_diffs_map[f.file_path] = f.diff_text
            if effective_repo:
                for variant in path_variants(effective_repo, session.cwd, f.file_path):
                    file_diffs_map[variant] = f.diff_text

    annotations = extract_annotations(session, file_diffs_map)
    normalize_annotation_paths(
        annotations, effective_repo, session.cwd,
        [f.file_path for f in diff_result.files] if diff_result else [],
    )

    # Step 6: Generate summary and title
    title = _generate_title(session)
    summary = _generate_summary(session, diff_result)

    # Step 7: Persist with upsert logic
    total_files = len(diff_result.files) if diff_result else 0
    total_additions = sum(f.additions for f in diff_result.files) if diff_result else 0
    total_deletions = sum(f.deletions for f in diff_result.files) if diff_result else 0

    # Upsert review — find existing by (repo_path, branch, base_ref)
    existing = None
    if effective_repo and effective_branch:
        existing = conn.execute(
            "SELECT id FROM reviews WHERE repo_path = ? AND branch = ? AND base_ref = ?",
            (effective_repo, effective_branch, base_ref),
        ).fetchone()

    if existing:
        review_id = existing["id"]
        # Clear old data
        conn.execute("DELETE FROM review_files WHERE review_id = ?", (review_id,))
        conn.execute("DELETE FROM annotations WHERE review_id = ?", (review_id,))
        # Update review metadata
        conn.execute(
            """UPDATE reviews SET
                session_id = ?,
                project = ?, title = ?, summary = ?,
                total_files_changed = ?, total_additions = ?, total_deletions = ?,
                session_date = ?, source_jsonl = ?, diff_hash = ?,
                generated_at = datetime('now')
               WHERE id = ?""",
            (session_id, project, title, summary,
             total_files, total_additions, total_deletions,
             session.first_timestamp, _container_to_host_path(str(jsonl_path)),
             diff_hash, review_id),
        )
    else:
        cursor = conn.execute(
            """INSERT INTO reviews
               (session_id, project, branch, repo_path, base_ref, title, summary,
                total_files_changed, total_additions, total_deletions,
                session_date, source_jsonl, diff_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, project, effective_branch, effective_repo, base_ref,
                title, summary, total_files, total_additions, total_deletions,
                session.first_timestamp,
                _container_to_host_path(str(jsonl_path)),
                diff_hash,
            ),
        )
        review_id = cursor.lastrowid

    # Persist files
    file_id_map: dict[str, int] = {}
    if diff_result:
        for i, fd in enumerate(diff_result.files):
            fc = conn.execute(
                """INSERT INTO review_files
                   (review_id, file_path, change_type, additions, deletions,
                    diff_text, full_content, language, move_metadata,
                    render_mode, pipeline_diff, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    review_id, fd.file_path, fd.change_type, fd.additions,
                    fd.deletions, fd.diff_text, fd.full_content, fd.language,
                    fd.move_metadata, fd.render_mode, fd.pipeline_diff, i,
                ),
            )
            file_id_map[fd.file_path] = fc.lastrowid

    # Persist annotations
    for ann in annotations:
        file_id = file_id_map.get(ann.file_path) if ann.file_path else None
        conn.execute(
            """INSERT INTO annotations
               (review_id, file_id, line_start, line_end, annotation_type,
                content, source_type, source_message_uuid, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                review_id, file_id, ann.line_start, ann.line_end,
                ann.annotation_type, ann.content, ann.source_type,
                ann.source_message_uuid, ann.sort_order,
            ),
        )

    conn.commit()
    return (review_id, "generated")


def generate_branch_review(
    conn: sqlite3.Connection,
    repo_path: str,
    branch: str,
    base_ref: str = "main",
    session_ids: list[str] | None = None,
) -> tuple[int, str]:
    """Generate (or update) a review showing the diff between base_ref and branch.

    This is the branch-centric flow: one review per (repo, branch, base_ref) triple,
    upserted on each call. If the diff hash is unchanged, returns existing review_id
    without regenerating.

    Accepts multiple session_ids to enrich the review with annotations from all
    related Claude sessions. Sessions are parsed most-recent-first with early
    stopping once the annotation budget is filled.

    Args:
        conn: SQLite connection
        repo_path: Host repo path (e.g., /Users/foo/Documents/my-app)
        branch: Branch with changes
        base_ref: Base ref to diff against (default: "main")
        session_ids: Optional list of Claude session UUIDs for annotation enrichment

    Returns:
        Tuple of (review_id, status) where status is "unchanged" or "generated"

    Raises:
        ValueError: If diff cannot be computed
    """
    # Step 1: Build diffs
    diff_result = build_diff(repo_path, branch, base_ref)
    if diff_result is None:
        raise ValueError(
            f"Cannot compute diff for {repo_path} ({base_ref}...{branch}). "
            "Check that the repo path, branch, and base ref are valid."
        )

    # Step 1.5: Check for existing review with same diff hash
    diff_hash = _compute_diff_hash(diff_result)
    session_ids_str = ",".join(session_ids) if session_ids else None
    existing = conn.execute(
        "SELECT id, diff_hash FROM reviews WHERE repo_path = ? AND branch = ? AND base_ref = ?",
        (repo_path, branch, base_ref),
    ).fetchone()

    if existing and existing["diff_hash"] == diff_hash:
        if session_ids_str:
            conn.execute(
                "UPDATE reviews SET session_id = ? WHERE id = ?",
                (session_ids_str, existing["id"]),
            )
            conn.commit()
        return (existing["id"], "unchanged")

    # Step 2: Parse sessions for annotations (multi-session with budget)
    # Build file diff map once — shared across all sessions
    file_diffs_map: dict[str, str] = {}
    for f in diff_result.files:
        file_diffs_map[f.file_path] = f.diff_text
        abs_path = os.path.join(repo_path, f.file_path)
        file_diffs_map[abs_path] = f.diff_text

    annotation_lists: list[list[Annotation]] = []
    first_session: ParsedSession | None = None

    for sid in (session_ids or []):
        jsonl_path = find_session_jsonl(sid)
        if not jsonl_path or not jsonl_path.exists():
            continue

        session = parse_session_with_subagents(jsonl_path)
        if first_session is None:
            first_session = session

        # The session may have run in another clone/worktree of the repo
        session_map = dict(file_diffs_map)
        for f in diff_result.files:
            for v in path_variants(repo_path, session.cwd, f.file_path):
                session_map[v] = f.diff_text

        session_annotations = extract_annotations(session, session_map)
        normalize_annotation_paths(
            session_annotations, repo_path, session.cwd,
            [f.file_path for f in diff_result.files],
        )

        annotation_lists.append(session_annotations)

        # Early stop: if we already have enough annotations, skip remaining sessions
        total_so_far = sum(len(al) for al in annotation_lists)
        if total_so_far >= 60:  # 2x budget gives merge_annotations room to dedup
            break

    annotations = merge_annotations(annotation_lists) if annotation_lists else []

    # Step 3: Generate title and summary
    project = Path(repo_path).name
    title = f"{branch} vs {base_ref}"
    total_files = len(diff_result.files)
    total_add = sum(f.additions for f in diff_result.files)
    total_del = sum(f.deletions for f in diff_result.files)
    summary = f"**Branch:** `{branch}` → `{base_ref}`\n\n"
    summary += f"{total_files} files changed, +{total_add} −{total_del}"

    sessions_parsed = len(annotation_lists)
    sessions_total = len(session_ids) if session_ids else 0
    if sessions_total > 0:
        summary += f"\n\n*Annotations from {sessions_parsed}/{sessions_total} sessions*"

    if first_session and first_session.turns:
        first_prompt = first_session.turns[0].user_text
        if first_prompt and len(first_prompt) > 30:
            summary = f"**Goal:** {first_prompt[:500]}\n\n" + summary

    # Step 5: Upsert review
    existing_review = conn.execute(
        "SELECT id FROM reviews WHERE repo_path = ? AND branch = ? AND base_ref = ?",
        (repo_path, branch, base_ref),
    ).fetchone()

    if existing_review:
        review_id = existing_review["id"]
        conn.execute("DELETE FROM review_files WHERE review_id = ?", (review_id,))
        conn.execute("DELETE FROM annotations WHERE review_id = ?", (review_id,))
        conn.execute(
            """UPDATE reviews SET
                session_id = COALESCE(?, session_id),
                project = ?, title = ?, summary = ?,
                total_files_changed = ?, total_additions = ?, total_deletions = ?,
                diff_hash = ?,
                generated_at = datetime('now')
               WHERE id = ?""",
            (session_ids_str, project, title, summary,
             total_files, total_add, total_del, diff_hash, review_id),
        )
    else:
        cursor = conn.execute(
            """INSERT INTO reviews
               (session_id, project, branch, repo_path, base_ref, title, summary,
                total_files_changed, total_additions, total_deletions, session_date, diff_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
            (session_ids_str, project, branch, repo_path, base_ref,
             title, summary, total_files, total_add, total_del, diff_hash),
        )
        review_id = cursor.lastrowid

    # Persist files
    file_id_map: dict[str, int] = {}
    for i, fd in enumerate(diff_result.files):
        fc = conn.execute(
            """INSERT INTO review_files
               (review_id, file_path, change_type, additions, deletions,
                diff_text, full_content, language, move_metadata,
                render_mode, pipeline_diff, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (review_id, fd.file_path, fd.change_type, fd.additions,
             fd.deletions, fd.diff_text, fd.full_content, fd.language,
             fd.move_metadata, fd.render_mode, fd.pipeline_diff, i),
        )
        file_id_map[fd.file_path] = fc.lastrowid

    # Persist annotations
    for ann in annotations:
        file_id = file_id_map.get(ann.file_path) if ann.file_path else None
        conn.execute(
            """INSERT INTO annotations
               (review_id, file_id, line_start, line_end, annotation_type,
                content, source_type, source_message_uuid, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (review_id, file_id, ann.line_start, ann.line_end,
             ann.annotation_type, ann.content, ann.source_type,
             ann.source_message_uuid, ann.sort_order),
        )

    conn.commit()
    return (review_id, "generated")


def list_available_sessions() -> list[dict]:
    """List sessions available for review generation.

    Discovers JSONL files directly from the mounted Claude directory.
    """
    sessions = []
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.exists():
        return sessions

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        project_name = _extract_project_name(
            next(project_dir.glob("*.jsonl"), project_dir / "dummy")
        )
        for jsonl in sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = jsonl.stat()
            sessions.append({
                "session_id": jsonl.stem,
                "project": project_name,
                "source_file": str(jsonl),
                "size_kb": round(stat.st_size / 1024),
                "mtime": stat.st_mtime,
            })

    return sessions[:100]  # Cap at 100
