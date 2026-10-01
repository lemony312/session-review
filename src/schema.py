"""SQLite schema for session reviews with annotated diffs."""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 5

DDL = """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    project TEXT NOT NULL,
    branch TEXT,
    repo_path TEXT,
    base_ref TEXT,
    title TEXT,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    total_files_changed INTEGER DEFAULT 0,
    total_additions INTEGER DEFAULT 0,
    total_deletions INTEGER DEFAULT 0,
    session_date TEXT,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    source_jsonl TEXT,
    diff_hash TEXT
);

CREATE TABLE IF NOT EXISTS review_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    change_type TEXT NOT NULL,
    additions INTEGER DEFAULT 0,
    deletions INTEGER DEFAULT 0,
    diff_text TEXT NOT NULL,
    full_content TEXT,
    language TEXT,
    move_metadata TEXT,
    render_mode TEXT DEFAULT 'diff',
    pipeline_diff TEXT,
    sort_order INTEGER DEFAULT 0,
    UNIQUE(review_id, file_path)
);

CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    file_id INTEGER REFERENCES review_files(id) ON DELETE CASCADE,
    line_start INTEGER,
    line_end INTEGER,
    annotation_type TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT,
    source_message_uuid TEXT,
    sort_order INTEGER DEFAULT 0
);

-- Legacy: no longer written or read; kept so existing databases still load.
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    source_message_uuid TEXT,
    sort_order INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS reviews_fts USING fts5(
    title,
    summary,
    project,
    content=reviews,
    content_rowid=id,
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS reviews_ai AFTER INSERT ON reviews BEGIN
    INSERT INTO reviews_fts(rowid, title, summary, project)
    VALUES (new.id, new.title, new.summary, new.project);
END;

CREATE TRIGGER IF NOT EXISTS reviews_ad AFTER DELETE ON reviews BEGIN
    INSERT INTO reviews_fts(reviews_fts, rowid, title, summary, project)
    VALUES ('delete', old.id, old.title, old.summary, old.project);
END;

CREATE TRIGGER IF NOT EXISTS reviews_au AFTER UPDATE ON reviews BEGIN
    INSERT INTO reviews_fts(reviews_fts, rowid, title, summary, project)
    VALUES ('delete', old.id, old.title, old.summary, old.project);
    INSERT INTO reviews_fts(rowid, title, summary, project)
    VALUES (new.id, new.title, new.summary, new.project);
END;

CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_branch
    ON reviews(repo_path, branch, base_ref)
    WHERE repo_path IS NOT NULL AND branch IS NOT NULL AND base_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MIGRATIONS = {
    2: [
        # Deduplicate existing reviews by (repo_path, branch, base_ref) — keep newest
        """
        DELETE FROM reviews WHERE id NOT IN (
            SELECT MAX(id) FROM reviews
            GROUP BY COALESCE(repo_path, ''), COALESCE(branch, ''), COALESCE(base_ref, '')
        );
        """,
        # Make session_id nullable for branch-only reviews
        # SQLite doesn't support ALTER COLUMN, so recreate the table.
        """
        CREATE TABLE IF NOT EXISTS reviews_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE,
            project TEXT NOT NULL,
            branch TEXT,
            repo_path TEXT,
            base_ref TEXT,
            title TEXT,
            summary TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            total_files_changed INTEGER DEFAULT 0,
            total_additions INTEGER DEFAULT 0,
            total_deletions INTEGER DEFAULT 0,
            session_date TEXT,
            generated_at TEXT NOT NULL DEFAULT (datetime('now')),
            source_jsonl TEXT
        );
        """,
        """INSERT OR IGNORE INTO reviews_new SELECT * FROM reviews;""",
        """DROP TABLE IF EXISTS reviews;""",
        """ALTER TABLE reviews_new RENAME TO reviews;""",
        # Re-create FTS triggers after table rename
        """DROP TRIGGER IF EXISTS reviews_ai;""",
        """DROP TRIGGER IF EXISTS reviews_ad;""",
        """DROP TRIGGER IF EXISTS reviews_au;""",
    ],
    3: [
        # Add diff_hash column for staleness detection
        """ALTER TABLE reviews ADD COLUMN diff_hash TEXT;""",
        # Drop session_id UNIQUE constraint by recreating table
        """
        CREATE TABLE IF NOT EXISTS reviews_v3 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            project TEXT NOT NULL,
            branch TEXT,
            repo_path TEXT,
            base_ref TEXT,
            title TEXT,
            summary TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            total_files_changed INTEGER DEFAULT 0,
            total_additions INTEGER DEFAULT 0,
            total_deletions INTEGER DEFAULT 0,
            session_date TEXT,
            generated_at TEXT NOT NULL DEFAULT (datetime('now')),
            source_jsonl TEXT,
            diff_hash TEXT
        );
        """,
        """INSERT INTO reviews_v3 SELECT id, session_id, project, branch, repo_path, base_ref, title, summary, status, total_files_changed, total_additions, total_deletions, session_date, generated_at, source_jsonl, diff_hash FROM reviews;""",
        """DROP TABLE reviews;""",
        """ALTER TABLE reviews_v3 RENAME TO reviews;""",
    ],
    4: [
        # Add move_metadata column to review_files
        """ALTER TABLE review_files ADD COLUMN move_metadata TEXT;""",
    ],
    5: [
        # Specialized renderer routing + computed pipeline (.pp) diff payload
        """ALTER TABLE review_files ADD COLUMN render_mode TEXT DEFAULT 'diff';""",
        """ALTER TABLE review_files ADD COLUMN pipeline_diff TEXT;""",
    ],
}


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize the database with schema, running migrations if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Check current schema version
    current_version = 0
    try:
        row = conn.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row:
            current_version = int(row["value"])
    except sqlite3.OperationalError:
        pass  # Table doesn't exist yet — fresh DB

    if current_version < SCHEMA_VERSION and current_version > 0:
        # Run migrations for existing DB
        # Use executescript per statement (each may contain multiple SQL lines)
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            if version in MIGRATIONS:
                for sql in MIGRATIONS[version]:
                    conn.executescript(sql.strip())
        conn.commit()

    # Apply full DDL (creates tables/indexes/triggers if missing)
    conn.executescript(DDL)
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn
