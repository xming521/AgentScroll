from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 2
DEFAULT_DATABASE_PATH = Path.cwd() / "outputs" / "agentscroll.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hotlist_topics (
    topic_id TEXT PRIMARY KEY,
    label TEXT NOT NULL CHECK (label IN ('news', 'fun')),
    status TEXT NOT NULL CHECK (status IN ('complete', 'needs_research', 'rejected')),
    last_result_status TEXT NOT NULL
        CHECK (last_result_status IN ('complete', 'needs_research', 'rejected')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title TEXT NOT NULL,
    knowledge TEXT NOT NULL,
    chat_context TEXT NOT NULL,
    latest_update TEXT,
    share_score REAL NOT NULL CHECK (share_score >= 0 AND share_score <= 4),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);

CREATE INDEX IF NOT EXISTS hotlist_topics_last_seen_idx
    ON hotlist_topics(last_seen_at);

CREATE TABLE IF NOT EXISTS hotlist_title_cache (
    normalized_title TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS hotlist_title_cache_last_seen_idx
    ON hotlist_title_cache(last_seen_at);

CREATE TABLE IF NOT EXISTS share_jobs (
    job_id TEXT PRIMARY KEY,
    share_group_id TEXT NOT NULL,
    topic_id TEXT,
    destination_id TEXT NOT NULL,
    transport TEXT NOT NULL,
    target TEXT NOT NULL,
    share_index INTEGER NOT NULL,
    score REAL NOT NULL CHECK (score >= 3 AND score <= 4),
    share_trigger TEXT NOT NULL
        CHECK (share_trigger IN ('normal', 'llm_major', 'hotlist_title_count')),
    due_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    bypass INTEGER NOT NULL CHECK (bypass IN (0, 1)),
    status TEXT NOT NULL
        CHECK (status IN (
            'waiting', 'inflight', 'sent', 'failed', 'unknown',
            'expired', 'superseded', 'dropped'
        )),
    reserved_at TEXT,
    finished_at TEXT,
    result_detail TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (share_group_id, destination_id, share_index)
);

CREATE INDEX IF NOT EXISTS share_jobs_pending_idx
    ON share_jobs(status, due_at);

CREATE INDEX IF NOT EXISTS share_jobs_destination_idx
    ON share_jobs(destination_id, bypass, status, reserved_at);

CREATE VIEW IF NOT EXISTS shared_content_review AS
SELECT
    jobs.finished_at AS shared_at,
    json_extract(jobs.payload_json, '$.title') AS title,
    json_extract(jobs.payload_json, '$.label') AS label,
    jobs.score AS share_score,
    json_extract(jobs.payload_json, '$.general_score') AS general_share_score,
    json_extract(jobs.payload_json, '$.interest_score') AS interest_share_score,
    json_extract(jobs.payload_json, '$.text') AS share_text,
    json_extract(jobs.payload_json, '$.url') AS source_url,
    json_extract(jobs.payload_json, '$.comment') AS comment,
    json_extract(jobs.payload_json, '$.comment_type') AS comment_type,
    json_extract(jobs.payload_json, '$.source_id') AS source_id,
    json_extract(jobs.payload_json, '$.comment_id') AS comment_id,
    jobs.transport,
    jobs.target,
    jobs.destination_id,
    jobs.topic_id,
    jobs.share_group_id,
    jobs.job_id
FROM share_jobs AS jobs
WHERE jobs.status = 'sent';
"""


def resolve_database_path(path: str | Path | None = None) -> Path:
    return Path(path or DEFAULT_DATABASE_PATH).expanduser().resolve()


def connect_database(path: str | Path | None = None) -> sqlite3.Connection:
    database_path = resolve_database_path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in {0, 1, SCHEMA_VERSION}:
        connection.close()
        raise ValueError(
            f"Unsupported AgentScroll database schema version: {version}"
        )
    if version == 1:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(share_jobs)")
        }
        if "share_trigger" not in columns:
            connection.execute(
                """
                ALTER TABLE share_jobs ADD COLUMN share_trigger TEXT NOT NULL
                    DEFAULT 'normal'
                    CHECK (share_trigger IN (
                        'normal', 'llm_major', 'hotlist_title_count'
                    ))
                """
            )
        connection.execute(
            """
            UPDATE share_jobs
            SET share_trigger = 'llm_major'
            WHERE CAST(json_extract(payload_json, '$.general_score') AS REAL) = 4
            """
        )
    connection.executescript(_SCHEMA)
    if version < SCHEMA_VERSION:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    return connection


@contextmanager
def database_transaction(
    path: str | Path | None = None,
    *,
    immediate: bool = False,
) -> Iterator[sqlite3.Connection]:
    connection = connect_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "DEFAULT_DATABASE_PATH",
    "SCHEMA_VERSION",
    "connect_database",
    "database_transaction",
    "resolve_database_path",
]
