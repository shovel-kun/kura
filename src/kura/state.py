from __future__ import annotations

import sqlite3
from pathlib import Path

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    source TEXT,
    started_at TEXT,
    ended_at TEXT,
    exit_code INTEGER,
    ytdlp_version TEXT,
    new_count INTEGER,
    log_path TEXT,
    failure_summary TEXT
);

CREATE TABLE IF NOT EXISTS video_failures (
    source TEXT,
    video_id TEXT,
    url TEXT,
    kind TEXT CHECK(kind IN ('permanent','transient')),
    attempts INTEGER,
    last_seen TEXT,
    last_error TEXT,
    PRIMARY KEY(source, video_id)
);

CREATE TABLE IF NOT EXISTS suppressions (
    source TEXT,
    archive_key TEXT,
    suppressed_at TEXT,
    reason TEXT,
    PRIMARY KEY(source, archive_key)
);

CREATE TABLE IF NOT EXISTS channel_cache (
    source TEXT PRIMARY KEY,
    channel_id TEXT,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS rss_feed_seen (
    source TEXT PRIMARY KEY,
    feed_ids TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY,
    last_check_at TEXT,
    last_success_at TEXT,
    imported_at TEXT,
    last_full_run_at TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    source TEXT,
    archive_key TEXT,
    video_id TEXT,
    title TEXT,
    uploader TEXT,
    upload_date TEXT,
    duration_s INTEGER,
    size_bytes INTEGER,
    dir TEXT,
    downloaded_at TEXT,
    PRIMARY KEY(source, archive_key)
);

CREATE TABLE IF NOT EXISTS video_index_state (
    source TEXT PRIMARY KEY,
    last_scan_at TEXT
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or paths.state_db()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
