"""FastAPI server for Session Review - annotated AI diff viewer."""

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from schema import init_db
import ask_client
from review_generator import generate_review, generate_branch_review, list_available_sessions, list_active_branches, find_session_jsonl

DB_PATH = Path(os.environ.get("DB_PATH", "/data/review.db"))
REPO_CONFIG_PATH = Path(os.environ.get("REPO_CONFIG_PATH", "/data/repo_config.json"))

DEFAULT_REPO_CONFIG = {"repo_defaults": {}}


def load_repo_config() -> dict:
    if REPO_CONFIG_PATH.exists():
        try:
            return json.loads(REPO_CONFIG_PATH.read_text())
        except Exception:
            pass
    return DEFAULT_REPO_CONFIG

conn = None


def get_conn():
    global conn
    if conn is None:
        conn = init_db(DB_PATH)
    return conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_conn()
    print(f"Session Review started. DB: {DB_PATH}")
    yield


app = FastAPI(title="Session Review", version="0.1.0", lifespan=lifespan)


# --- API Routes ---

@app.get("/health")
def health():
    db = get_conn()
    review_count = db.execute("SELECT COUNT(*) as c FROM reviews").fetchone()["c"]
    file_count = db.execute("SELECT COUNT(*) as c FROM review_files").fetchone()["c"]
    annotation_count = db.execute("SELECT COUNT(*) as c FROM annotations").fetchone()["c"]
    return {
        "status": "healthy",
        "reviews": review_count,
        "files": file_count,
        "annotations": annotation_count,
    }


@app.get("/api/config")
def api_config():
    """Get repo configuration for the UI."""
    return load_repo_config()


@app.put("/api/config")
def api_update_config(config: dict):
    """Update repo configuration."""
    try:
        REPO_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPO_CONFIG_PATH.write_text(json.dumps(config, indent=2))
        return {"status": "updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sessions")
def api_list_sessions(
    project: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """List available sessions for review generation."""
    sessions = list_available_sessions()
    if project:
        sessions = [s for s in sessions if s.get("project") == project]
    return {"sessions": sessions[:limit]}


@app.get("/api/branches")
def api_list_branches():
    """List active branches with their correlated sessions."""
    db = get_conn()
    config = load_repo_config()
    branches = list_active_branches(
        repo_config=config.get("repo_defaults"),
        conn=db,
    )
    return {"branches": branches}


@app.post("/api/reviews/generate")
def api_generate_review(
    session_id: str = Query(..., description="Session UUID"),
    base_ref: str = Query("main", description="Base ref to diff against"),
    repo_path: str = Query(None, description="Host repo path override"),
    branch: str = Query(None, description="Branch override"),
):
    """Generate a review for a session.

    Returns status "unchanged" if the diff hasn't changed since last generation,
    or "generated" if the review was created/updated.
    """
    db = get_conn()
    try:
        review_id, status = generate_review(
            conn=db,
            session_id=session_id,
            repo_path=repo_path,
            branch=branch,
            base_ref=base_ref,
        )
        return {"review_id": review_id, "status": status}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation failed: {e}")


@app.post("/api/reviews/generate-branch")
def api_generate_branch_review(
    repo_path: str = Query(..., description="Host repo path"),
    branch: str = Query(..., description="Branch with changes"),
    base_ref: str = Query("main", description="Base ref to diff against"),
    session_ids: str = Query(None, description="Comma-separated session UUIDs for annotations"),
):
    """Generate or update a review for a branch diff.

    Returns a stable review_id for the same (repo, branch, base_ref) triple.
    Returns status "unchanged" if the diff hasn't changed since last generation,
    or "generated" if the review was created/updated.
    """
    db = get_conn()
    sid_list = [s.strip() for s in session_ids.split(",") if s.strip()] if session_ids else None
    try:
        review_id, status = generate_branch_review(
            conn=db,
            repo_path=repo_path,
            branch=branch,
            base_ref=base_ref,
            session_ids=sid_list,
        )
        return {"review_id": review_id, "status": status}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation failed: {e}")


@app.get("/api/reviews")
def api_list_reviews(
    project: str = Query(None),
    status: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """List all generated reviews."""
    db = get_conn()
    sql = "SELECT * FROM reviews WHERE 1=1"
    params: list = []

    if project:
        sql += " AND project = ?"
        params.append(project)
    if status:
        sql += " AND status = ?"
        params.append(status)

    sql += " ORDER BY generated_at DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    return {
        "reviews": [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "project": r["project"],
                "branch": r["branch"],
                "title": r["title"],
                "status": r["status"],
                "total_files_changed": r["total_files_changed"],
                "total_additions": r["total_additions"],
                "total_deletions": r["total_deletions"],
                "session_date": r["session_date"],
                "generated_at": r["generated_at"],
            }
            for r in rows
        ]
    }


@app.get("/api/reviews/{review_id}")
def api_get_review(review_id: int):
    """Get a full review with files, and annotations."""
    db = get_conn()

    review = db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    files = db.execute(
        "SELECT * FROM review_files WHERE review_id = ? ORDER BY sort_order",
        (review_id,),
    ).fetchall()

    annotations = db.execute(
        "SELECT * FROM annotations WHERE review_id = ? ORDER BY sort_order",
        (review_id,),
    ).fetchall()

    return {
        "review": {
            "id": review["id"],
            "session_id": review["session_id"],
            "project": review["project"],
            "branch": review["branch"],
            "repo_path": review["repo_path"],
            "base_ref": review["base_ref"],
            "title": review["title"],
            "summary": review["summary"],
            "status": review["status"],
            "total_files_changed": review["total_files_changed"],
            "total_additions": review["total_additions"],
            "total_deletions": review["total_deletions"],
            "session_date": review["session_date"],
            "generated_at": review["generated_at"],
            "source_jsonl": review["source_jsonl"],
        },
        "files": [
            {
                "id": f["id"],
                "file_path": f["file_path"],
                "change_type": f["change_type"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "diff_text": f["diff_text"],
                "full_content": f["full_content"],
                "language": f["language"],
                "move_metadata": f["move_metadata"],
                "render_mode": f["render_mode"],
                "pipeline_diff": f["pipeline_diff"],
            }
            for f in files
        ],
        "annotations": [
            {
                "id": a["id"],
                "file_id": a["file_id"],
                "line_start": a["line_start"],
                "line_end": a["line_end"],
                "annotation_type": a["annotation_type"],
                "content": a["content"],
                "source_type": a["source_type"],
            }
            for a in annotations
        ],
    }


@app.delete("/api/reviews/{review_id}")
def api_delete_review(review_id: int):
    """Delete a review."""
    db = get_conn()
    db.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
    db.commit()
    return {"status": "deleted"}


@app.patch("/api/reviews/{review_id}")
def api_update_review(review_id: int, title: str = None, status: str = None):
    """Update review metadata."""
    db = get_conn()
    updates = []
    params = []
    if title is not None:
        updates.append("title = ?")
        params.append(title)
    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    params.append(review_id)
    db.execute(f"UPDATE reviews SET {', '.join(updates)} WHERE id = ?", params)
    db.commit()
    return {"status": "updated"}


# --- Model Selection & Context Loading ---

# CLI model aliases (the claude CLI resolves them to the latest model)
MODELS = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"}
MODEL_LABELS = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}

DEEP_REASONING_KEYWORDS = [
    "why", "explain", "trade-off", "tradeoff", "architecture", "design decision",
    "how does this relate", "reasoning", "approach", "motivation", "rationale",
]


def select_model(scope: str, question: str, user_override: str | None = None) -> tuple[str, str]:
    """Returns (cli_alias, label) based on scope and question complexity."""
    if user_override in MODELS:
        alias = user_override
    elif any(kw in question.lower() for kw in DEEP_REASONING_KEYWORDS) or scope == "review":
        alias = "opus"
    elif scope == "file":
        alias = "sonnet"
    else:
        alias = "haiku"
    return MODELS[alias], MODEL_LABELS[alias]


def host_to_container_path(host_path: str) -> Path:
    """Convert host repo path to container path. /Users/.../Documents/my-app -> /repos/my-app"""
    repos_host_dir = os.environ.get("REPOS_HOST_DIR", "")
    repos_dir = Path(os.environ.get("REPOS_DIR", "/repos"))
    if repos_host_dir and host_path.startswith(repos_host_dir):
        relative = host_path[len(repos_host_dir):].lstrip("/")
        return repos_dir / relative
    return repos_dir / Path(host_path).name


def load_repo_context(repo_path: str, scope: str) -> str:
    """Load CLAUDE.md and domain context files from the repo."""
    container_path = host_to_container_path(repo_path)
    parts = []

    # Always try CLAUDE.md for file and review scope
    if scope in ("file", "review"):
        claude_md = container_path / ".claude" / "CLAUDE.md"
        if claude_md.exists():
            parts.append(f"## Repository Instructions (CLAUDE.md)\n{claude_md.read_text()[:8000]}")

    # CONTEXT.md only for review scope (it's large)
    if scope == "review":
        context_md = container_path / ".claude" / "CONTEXT.md"
        if context_md.exists():
            parts.append(f"## Repository Context (CONTEXT.md)\n{context_md.read_text()[:30000]}")

    # Domain context files
    if scope == "review":
        project_name = Path(repo_path).name
        domains_env = os.environ.get("DOMAIN_CONTEXT_DIR")
        domains_dir = Path(domains_env) if domains_env else None
        if domains_dir and domains_dir.exists():
            for md_file in domains_dir.iterdir():
                if md_file.suffix == ".md" and project_name.lower() in md_file.stem.lower():
                    parts.append(f"## Domain Context ({md_file.name})\n{md_file.read_text()[:8000]}")

    return "\n\n".join(parts)


def load_annotations_context(db, review_id: int, file_id: int | None, scope: str) -> str:
    """Load relevant annotations from the DB."""
    if scope == "line" and file_id:
        rows = db.execute(
            "SELECT annotation_type, content, source_type FROM annotations WHERE review_id = ? AND file_id = ?",
            (review_id, file_id)
        ).fetchall()
    elif scope == "file" and file_id:
        rows = db.execute(
            "SELECT annotation_type, content, source_type FROM annotations WHERE review_id = ? AND (file_id = ? OR file_id IS NULL)",
            (review_id, file_id)
        ).fetchall()
    else:  # review scope
        rows = db.execute(
            "SELECT annotation_type, content, source_type FROM annotations WHERE review_id = ?",
            (review_id,)
        ).fetchall()

    if not rows:
        return ""

    parts = ["## Session Annotations (AI reasoning about changes)"]
    for r in rows:
        parts.append(f"[{r['annotation_type']}] ({r['source_type']}): {r['content']}")
    return "\n".join(parts)


def estimate_tokens(text: str) -> int:
    return len(text) // 4


# --- Ask Claude Endpoint ---

class AskRequest(BaseModel):
    review_id: int
    file_id: int | None = None      # Optional for review-scope
    line_number: int | None = None   # Optional for file/review-scope
    question: str
    context: str = ""                # Diff context from frontend
    scope: str = "line"              # "line", "file", or "review"
    model: str | None = None         # User override: "opus", "sonnet", "haiku"


@app.post("/api/ask")
def api_ask(req: AskRequest):
    """Ask the host agent CLI (claude by default) a question about a review, via the bridge."""
    db = get_conn()

    # 1. Look up the review
    review = db.execute(
        "SELECT id, repo_path, project, branch, base_ref, summary FROM reviews WHERE id = ?",
        (req.review_id,),
    ).fetchone()
    if not review:
        return {"error": True, "response": "Review not found."}

    repo_path = review["repo_path"] or ""
    review_summary = review["summary"] or ""

    # 2. Select model
    cli_model, model_label = select_model(req.scope, req.question, req.model)
    logger.info(f"Ask endpoint: scope={req.scope}, model={model_label}, review={req.review_id}")

    # 3. Build system prompt
    system_msg = (
        "You are an expert code reviewer with deep knowledge of the codebase. "
        "You have tools to read files and search code in the repository (your working directory). "
        "USE THEM to answer questions — don't guess or say you lack context when you can look it up. "
        "Answer questions about code changes using the provided context (diffs, annotations, repo docs) AND by exploring the repo with your tools. "
        "Keep responses concise (1-4 paragraphs) and use markdown formatting. "
        "When referencing AI annotations, cite them naturally (e.g., 'The session notes indicate...'). "
        "Never suggest running commands or ask the user follow-up questions. "
        "Give definitive answers by using your tools to verify. Never ask the user to provide more information."
    )

    # 4. Build user message with layered context
    user_msg_parts = []

    # Review header
    user_msg_parts.append(
        f"# Review: {review['project']} ({review['branch'] or 'unknown branch'})"
    )

    # Look up the specific file if file_id is provided
    file_row = None
    if req.file_id:
        file_row = db.execute(
            "SELECT file_path, diff_text, full_content, language FROM review_files WHERE id = ? AND review_id = ?",
            (req.file_id, req.review_id),
        ).fetchone()

    if req.scope == "line":
        # Line scope: file diff + 60-line window + file annotations + review summary
        if file_row:
            file_path = file_row["file_path"]
            language = file_row["language"] or ""
            diff_text = file_row["diff_text"] or ""
            full_content = file_row["full_content"] or ""

            user_msg_parts.append(f"File: {file_path} (line {req.line_number})")
            if language:
                user_msg_parts.append(f"Language: {language}")

            if req.context:
                user_msg_parts.append(
                    f"\n## Diff context around line {req.line_number}\n```\n{req.context}\n```"
                )

            if diff_text:
                user_msg_parts.append(f"\n## Full file diff\n```diff\n{diff_text[:10000]}\n```")

            if full_content and req.line_number:
                lines = full_content.split("\n")
                start = max(0, req.line_number - 30)
                end = min(len(lines), req.line_number + 30)
                snippet = "\n".join(lines[start:end])
                user_msg_parts.append(
                    f"\n## Full file context (lines {start + 1}-{end})\n```{language}\n{snippet}\n```"
                )
        else:
            user_msg_parts.append("(No file context available)")

        # File-level annotations
        annotations_ctx = load_annotations_context(db, req.review_id, req.file_id, "line")
        if annotations_ctx:
            user_msg_parts.append(f"\n{annotations_ctx}")

        # Brief review summary
        if review_summary:
            user_msg_parts.append(f"\n## Review Summary\n{review_summary[:2000]}")

    elif req.scope == "file":
        # File scope: full diff + full_content + file annotations + review summary + CLAUDE.md
        if file_row:
            file_path = file_row["file_path"]
            language = file_row["language"] or ""
            diff_text = file_row["diff_text"] or ""
            full_content = file_row["full_content"] or ""

            user_msg_parts.append(f"File: {file_path}")
            if language:
                user_msg_parts.append(f"Language: {language}")

            if diff_text:
                user_msg_parts.append(f"\n## Full file diff\n```diff\n{diff_text[:30000]}\n```")

            if full_content:
                user_msg_parts.append(
                    f"\n## Full file content\n```{language}\n{full_content[:50000]}\n```"
                )

            if req.context:
                user_msg_parts.append(f"\n## Selected diff context\n```\n{req.context}\n```")
        else:
            user_msg_parts.append("(No file context available)")

        # Other files in this review (so Claude can cross-reference moves/renames)
        other_files = db.execute(
            "SELECT file_path, change_type, additions, deletions, diff_text FROM review_files WHERE review_id = ? AND id != ? ORDER BY sort_order",
            (req.review_id, req.file_id),
        ).fetchall()
        if other_files:
            other_parts = ["\n## Other files changed in this review"]
            for of in other_files:
                diff_preview = (of["diff_text"] or "")[:3000]
                other_parts.append(
                    f"\n### {of['file_path']} ({of['change_type']}, +{of['additions']} -{of['deletions']})\n```diff\n{diff_preview}\n```"
                )
            user_msg_parts.append("\n".join(other_parts))

        # File + review-level annotations
        annotations_ctx = load_annotations_context(db, req.review_id, req.file_id, "file")
        if annotations_ctx:
            user_msg_parts.append(f"\n{annotations_ctx}")

        # Review summary
        if review_summary:
            user_msg_parts.append(f"\n## Review Summary\n{review_summary[:3000]}")

        # Repo context (CLAUDE.md)
        if repo_path:
            repo_ctx = load_repo_context(repo_path, "file")
            if repo_ctx:
                user_msg_parts.append(f"\n{repo_ctx}")

    elif req.scope == "review":
        # Review scope: all file diffs (capped) + all annotations + full repo context
        all_files = db.execute(
            "SELECT file_path, diff_text, language FROM review_files WHERE review_id = ? ORDER BY sort_order",
            (req.review_id,),
        ).fetchall()

        if all_files:
            user_msg_parts.append("\n## Changed Files")
            for f in all_files:
                diff = (f["diff_text"] or "")[:6000]
                lang = f["language"] or ""
                user_msg_parts.append(f"\n### {f['file_path']}\n```diff\n{diff}\n```")

        # All annotations
        annotations_ctx = load_annotations_context(db, req.review_id, None, "review")
        if annotations_ctx:
            user_msg_parts.append(f"\n{annotations_ctx}")

        # Review summary
        if review_summary:
            user_msg_parts.append(f"\n## Review Summary\n{review_summary}")

        # Full repo context (CLAUDE.md + CONTEXT.md + domain files)
        if repo_path:
            repo_ctx = load_repo_context(repo_path, "review")
            if repo_ctx:
                user_msg_parts.append(f"\n{repo_ctx}")

    # Always append the question last
    user_msg_parts.append(f"\n## Question\n{req.question}")

    user_msg = "\n".join(p for p in user_msg_parts if p)

    # 5. Token budget check - truncate if over 200k
    token_budget = 200_000
    estimated = estimate_tokens(system_msg + user_msg)
    logger.info(f"Ask endpoint: estimated tokens={estimated}, model={model_label}")

    if estimated > token_budget:
        # Truncate the user message to fit
        max_chars = token_budget * 4 - len(system_msg)
        user_msg = user_msg[:max_chars]
        logger.warning(f"Ask endpoint: truncated context from {estimated} to ~{token_budget} tokens")

    # 6. Ask the host's claude CLI via the bridge (read-only tools)
    if not repo_path:
        return {"error": True, "response": "This review has no repository path."}
    result = ask_client.ask_claude(repo_path, f"{system_msg}\n\n{user_msg}", cli_model)
    if result.get("error"):
        result.setdefault("response", result.get("message", "Ask failed."))
        result["model_used"] = model_label
        return result
    agent = result.get("agent") or "claude"
    # the opus/sonnet/haiku picker only applies to claude; other agents show their own name
    return {"error": False, "response": result.get("answer") or "(No response generated)",
            "model_used": model_label if agent == "claude" else (result.get("name") or agent),
            "source": f"{agent}-cli"}


@app.get("/api/ask/status")
def api_ask_status():
    """Whether Ask can run (bridge up and its agent CLI installed), and which agent."""
    info = ask_client.bridge_info()
    ok = bool(info and info.get("available"))
    who = info if info and info.get("name") else ask_client._last_agent
    return {"available": ok, "agent": who.get("agent"), "name": who.get("name"), "binary": who.get("binary"),
            "message": None if ok else ask_client.not_available_message(who)}


# --- Static Files ---

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    """Serve the main page."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return HTMLResponse("<h1>Session Review</h1><p>Static files not found.</p>")


@app.get("/review/{review_id}", response_class=HTMLResponse)
def review_page(review_id: int):
    """Serve the review detail page."""
    review_file = STATIC_DIR / "review.html"
    if review_file.exists():
        return FileResponse(str(review_file))
    return HTMLResponse(f"<h1>Review {review_id}</h1><p>Static files not found.</p>")
