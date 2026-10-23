from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    spotify_id       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    owner            TEXT,
    description      TEXT,
    snapshot_id      TEXT,
    total            INTEGER,
    fetched_at       TEXT,
    yt_playlist_id   TEXT,
    yt_playlist_name TEXT
);

CREATE TABLE IF NOT EXISTS tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id TEXT NOT NULL REFERENCES playlists(spotify_id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    spotify_id  TEXT,
    title       TEXT NOT NULL,
    artists     TEXT NOT NULL,
    album       TEXT,
    duration_ms INTEGER,
    isrc        TEXT,
    explicit    INTEGER NOT NULL DEFAULT 0,
    is_local    INTEGER NOT NULL DEFAULT 0,
    added_at    TEXT,
    UNIQUE (playlist_id, position)
);

CREATE TABLE IF NOT EXISTS matches (
    track_id    INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    video_id    TEXT,
    yt_title    TEXT,
    yt_artists  TEXT,
    yt_album    TEXT,
    yt_duration INTEGER,
    score       REAL,
    status      TEXT NOT NULL DEFAULT 'pending',
    candidates  TEXT,
    note        TEXT,
    updated_at  TEXT,
    added_at    TEXT
);

CREATE TABLE IF NOT EXISTS search_cache (
    key        TEXT PRIMARY KEY,
    query      TEXT,
    results    TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracks_playlist ON tracks(playlist_id);
CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status);
CREATE INDEX IF NOT EXISTS idx_matches_video ON matches(video_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
