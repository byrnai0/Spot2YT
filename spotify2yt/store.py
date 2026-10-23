from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from rapidfuzz import fuzz, process

from .errors import PlaylistError
from .matching import Candidate, TrackInfo, normalize
from .util import extract_spotify_id, json_load, utcnow


# ---------------------------------------------------------------------------
# Playlists and tracks
# ---------------------------------------------------------------------------


def upsert_playlist(conn: sqlite3.Connection, meta: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO playlists
            (spotify_id, name, owner, description, snapshot_id, total, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(spotify_id) DO UPDATE SET
            name = excluded.name,
            owner = excluded.owner,
            description = excluded.description,
            snapshot_id = excluded.snapshot_id,
            total = excluded.total,
            fetched_at = excluded.fetched_at
        """,
        (
            meta["id"],
            meta.get("name") or "(untitled)",
            meta.get("owner"),
            meta.get("description"),
            meta.get("snapshot_id"),
            meta.get("total"),
            utcnow(),
        ),
    )
    conn.commit()


def replace_tracks(
    conn: sqlite3.Connection, playlist_id: str, tracks: list[dict[str, Any]]
) -> tuple[int, int, int]:
    """Upsert tracks by position. Existing matches survive a re-export."""
    existing = {
        row["position"]: row
        for row in conn.execute(
            "SELECT id, position, spotify_id FROM tracks WHERE playlist_id = ?",
            (playlist_id,),
        )
    }

    inserted = updated = 0
    now = utcnow()
    for track in tracks:
        row = existing.get(track["position"])
        if row is None:
            cursor = conn.execute(
                """
                INSERT INTO tracks
                    (playlist_id, position, spotify_id, title, artists, album,
                     duration_ms, isrc, explicit, is_local, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    playlist_id,
                    track["position"],
                    track["spotify_id"],
                    track["title"],
                    json.dumps(track["artists"], ensure_ascii=False),
                    track.get("album"),
                    track.get("duration_ms"),
                    track.get("isrc"),
                    int(track.get("explicit") or 0),
                    int(track.get("is_local") or 0),
                    track.get("added_at"),
                ),
            )
            conn.execute(
                "INSERT INTO matches (track_id, status, updated_at) VALUES (?, 'pending', ?)",
                (cursor.lastrowid, now),
            )
            inserted += 1
            continue

        if (row["spotify_id"] or "") != (track["spotify_id"] or ""):
            # Position now holds a different song: old match is meaningless.
            conn.execute("DELETE FROM matches WHERE track_id = ?", (row["id"],))
            conn.execute(
                "INSERT INTO matches (track_id, status, updated_at) VALUES (?, 'pending', ?)",
                (row["id"], now),
            )

        conn.execute(
            """
            UPDATE tracks SET
                spotify_id = ?, title = ?, artists = ?, album = ?,
                duration_ms = ?, isrc = ?, explicit = ?, is_local = ?, added_at = ?
            WHERE id = ?
            """,
            (
                track["spotify_id"],
                track["title"],
                json.dumps(track["artists"], ensure_ascii=False),
                track.get("album"),
                track.get("duration_ms"),
                track.get("isrc"),
                int(track.get("explicit") or 0),
                int(track.get("is_local") or 0),
                track.get("added_at"),
                row["id"],
            ),
        )
        updated += 1

    positions = [track["position"] for track in tracks]
    if positions:
        placeholders = ",".join("?" for _ in positions)
        cursor = conn.execute(
            f"DELETE FROM tracks WHERE playlist_id = ? AND position NOT IN ({placeholders})",
            (playlist_id, *positions),
        )
    else:
        cursor = conn.execute("DELETE FROM tracks WHERE playlist_id = ?", (playlist_id,))
    removed = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    conn.commit()
    return inserted, updated, removed


def ensure_matches(conn: sqlite3.Connection, playlist_id: str) -> None:
    conn.execute(
        """
        INSERT INTO matches (track_id, status, updated_at)
        SELECT t.id, 'pending', ?
        FROM tracks t
        LEFT JOIN matches m ON m.track_id = t.id
        WHERE t.playlist_id = ? AND m.track_id IS NULL
        """,
        (utcnow(), playlist_id),
    )
    conn.commit()


def track_info(row: sqlite3.Row | dict[str, Any]) -> TrackInfo:
    artists = row["artists"]
    if isinstance(artists, str):
        artists = json_load(artists, [])
    return TrackInfo(
        title=row["title"],
        artists=list(artists or []),
        album=row["album"],
        duration_ms=row["duration_ms"],
    )


def resolve_local_playlist(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    ref = (ref or "").strip()
    exact_id = conn.execute(
        "SELECT * FROM playlists WHERE spotify_id = ?", (ref,)
    ).fetchone()
    if exact_id:
        return dict(exact_id)

    spotify_id = extract_spotify_id(ref)
    if spotify_id:
        row = conn.execute(
            "SELECT * FROM playlists WHERE spotify_id = ?", (spotify_id,)
        ).fetchone()
        if row:
            return dict(row)
        raise PlaylistError(
            f"Spotify playlist {spotify_id} is not in the local database yet. "
            f"Run `s2y export {spotify_id}` first."
        )

    rows = conn.execute("SELECT * FROM playlists ORDER BY name").fetchall()
    if not rows:
        raise PlaylistError(
            "The local database is empty. Run `s2y playlists`, then "
            "`s2y export <playlist>`."
        )

    exact = [row for row in rows if (row["name"] or "").casefold() == ref.casefold()]
    if len(exact) == 1:
        return dict(exact[0])
    if len(exact) > 1:
        listing = "\n".join(f"  {row['spotify_id']}  {row['name']}" for row in exact)
        raise PlaylistError(
            f"{len(exact)} local playlists are named {ref!r}. Use the ID:\n{listing}"
        )

    names = [row["name"] for row in rows]
    matches = process.extract(ref, names, scorer=fuzz.WRatio, limit=5)
    if matches and matches[0][1] >= 80:
        name = matches[0][0]
        return dict(next(row for row in rows if row["name"] == name))

    listing = "\n".join(f"  {row['spotify_id']}  {row['name']}" for row in rows[:30])
    raise PlaylistError(
        f"No local playlist matched {ref!r}. Export it first.\n\n{listing}"
    )


def list_local_playlists(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT p.*, COUNT(t.id) AS track_count
            FROM playlists p LEFT JOIN tracks t ON t.playlist_id = p.spotify_id
            GROUP BY p.spotify_id ORDER BY p.name
            """
        )
    ]


def set_yt_target(
    conn: sqlite3.Connection,
    playlist_id: str,
    yt_playlist_id: str,
    yt_playlist_name: str,
) -> None:
    conn.execute(
        """
        UPDATE playlists
        SET yt_playlist_id = ?, yt_playlist_name = ?
        WHERE spotify_id = ?
        """,
        (yt_playlist_id, yt_playlist_name, playlist_id),
    )
    conn.commit()


def clear_yt_target(conn: sqlite3.Connection, playlist_id: str) -> None:
    conn.execute(
        "UPDATE playlists SET yt_playlist_id = NULL WHERE spotify_id = ?",
        (playlist_id,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Matching state
# ---------------------------------------------------------------------------


def matching_rows(
    conn: sqlite3.Connection, playlist_id: str, force: bool = False
) -> list[sqlite3.Row]:
    ensure_matches(conn, playlist_id)
    force_clause = "OR (? = 1 AND m.status != 'added')" if force else ""
    params: list[Any] = [playlist_id]
    if force:
        params.append(1)
    return list(
        conn.execute(
            f"""
            SELECT t.*, m.status, m.score, m.video_id, m.note
            FROM tracks t
            LEFT JOIN matches m ON m.track_id = t.id
            WHERE t.playlist_id = ?
              AND (m.track_id IS NULL OR m.status IN ('pending', 'unmatched', 'error')
                   {force_clause})
            ORDER BY t.position
            """,
            params,
        )
    )


def save_match(
    conn: sqlite3.Connection,
    track_id: int,
    status: str,
    candidates: Iterable[Candidate],
    best: Candidate | None,
    note: str | None = None,
) -> None:
    payload = json.dumps(
        [candidate.to_dict() for candidate in candidates], ensure_ascii=False
    )
    if best is None:
        conn.execute(
            """
            UPDATE matches SET
                video_id = NULL, yt_title = NULL, yt_artists = NULL,
                yt_album = NULL, yt_duration = NULL, score = NULL,
                status = ?, candidates = ?, note = ?, updated_at = ?
            WHERE track_id = ?
            """,
            (status, payload, note, utcnow(), track_id),
        )
    else:
        conn.execute(
            """
            UPDATE matches SET
                video_id = ?, yt_title = ?, yt_artists = ?, yt_album = ?,
                yt_duration = ?, score = ?, status = ?, candidates = ?,
                note = ?, updated_at = ?
            WHERE track_id = ?
            """,
            (
                best.video_id,
                best.title,
                json.dumps(best.artists, ensure_ascii=False),
                best.album,
                best.duration,
                best.score,
                status,
                payload,
                note,
                utcnow(),
                track_id,
            ),
        )
    conn.commit()


def save_error(conn: sqlite3.Connection, track_id: int, message: str) -> None:
    conn.execute(
        "UPDATE matches SET status = 'error', note = ?, updated_at = ? WHERE track_id = ?",
        (message[:1000], utcnow(), track_id),
    )
    conn.commit()


def sync_rows(conn: sqlite3.Connection, playlist_id: str) -> list[sqlite3.Row]:
    ensure_matches(conn, playlist_id)
    return list(
        conn.execute(
            """
            SELECT t.*, m.video_id, m.yt_title, m.yt_artists, m.score, m.status
            FROM tracks t
            JOIN matches m ON m.track_id = t.id
            WHERE t.playlist_id = ?
              AND m.status IN ('auto', 'manual', 'added', 'failed', 'duplicate')
              AND m.video_id IS NOT NULL
            ORDER BY t.position
            """,
            (playlist_id,),
        )
    )


def mark_added(conn: sqlite3.Connection, track_id: int) -> None:
    conn.execute(
        "UPDATE matches SET status = 'added', added_at = ?, note = NULL WHERE track_id = ?",
        (utcnow(), track_id),
    )


def mark_failed(conn: sqlite3.Connection, track_id: int, message: str) -> None:
    conn.execute(
        "UPDATE matches SET status = 'failed', note = ?, updated_at = ? WHERE track_id = ?",
        (message[:1000], utcnow(), track_id),
    )


def mark_duplicate(conn: sqlite3.Connection, track_id: int, note: str) -> None:
    conn.execute(
        "UPDATE matches SET status = 'duplicate', note = ?, updated_at = ? WHERE track_id = ?",
        (note[:1000], utcnow(), track_id),
    )


def mark_unmatched(conn: sqlite3.Connection, track_id: int, note: str | None = None) -> None:
    conn.execute(
        "UPDATE matches SET status = 'unmatched', note = ?, updated_at = ? WHERE track_id = ?",
        (note, utcnow(), track_id),
    )
    conn.commit()


def set_manual(
    conn: sqlite3.Connection,
    track_id: int,
    video_id: str,
    candidate: dict[str, Any] | None,
) -> None:
    if candidate:
        conn.execute(
            """
            UPDATE matches SET
                video_id = ?, yt_title = ?, yt_artists = ?, yt_album = ?,
                yt_duration = ?, score = 1.0, status = 'manual',
                note = 'manual selection', updated_at = ?
            WHERE track_id = ?
            """,
            (
                video_id,
                candidate.get("title"),
                json.dumps(candidate.get("artists") or [], ensure_ascii=False),
                candidate.get("album"),
                candidate.get("duration"),
                utcnow(),
                track_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE matches SET
                video_id = ?, yt_title = '(manual selection)', yt_artists = '[]',
                yt_album = NULL, yt_duration = NULL, score = 1.0,
                status = 'manual', note = 'manual video id', updated_at = ?
            WHERE track_id = ?
            """,
            (video_id, utcnow(), track_id),
        )
    conn.commit()


def status_counts(conn: sqlite3.Connection, playlist_id: str) -> dict[str, int]:
    ensure_matches(conn, playlist_id)
    rows = conn.execute(
        """
        SELECT COALESCE(m.status, 'pending') AS status, COUNT(*) AS n
        FROM tracks t LEFT JOIN matches m ON m.track_id = t.id
        WHERE t.playlist_id = ?
        GROUP BY COALESCE(m.status, 'pending')
        """,
        (playlist_id,),
    )
    return {row["status"]: row["n"] for row in rows}


def review_rows(conn: sqlite3.Connection, playlist_id: str) -> list[sqlite3.Row]:
    ensure_matches(conn, playlist_id)
    return list(
        conn.execute(
            """
            SELECT t.position, t.title, t.artists, t.album, t.duration_ms,
                   m.status, m.score, m.video_id, m.yt_title, m.yt_artists,
                   m.candidates, m.note
            FROM tracks t JOIN matches m ON m.track_id = t.id
            WHERE t.playlist_id = ? AND m.status IN ('review', 'unmatched')
            ORDER BY t.position
            """,
            (playlist_id,),
        )
    )


def candidates_for_track(
    conn: sqlite3.Connection, playlist_id: str, position: int
) -> list[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT m.candidates
        FROM tracks t JOIN matches m ON m.track_id = t.id
        WHERE t.playlist_id = ? AND t.position = ?
        """,
        (playlist_id, position),
    ).fetchone()
    if not row:
        return []
    data = json_load(row["candidates"], [])
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Search cache
# ---------------------------------------------------------------------------


def cache_key(query: str, filter_name: str | None) -> str:
    return f"{filter_name or 'any'}::{normalize(query)}"


def cache_get(
    conn: sqlite3.Connection, key: str, ttl_days: int
) -> list[dict[str, Any]] | None:
    if ttl_days <= 0:
        return None
    row = conn.execute(
        "SELECT results, created_at FROM search_cache WHERE key = ?", (key,)
    ).fetchone()
    if not row:
        return None
    try:
        created = datetime.fromisoformat(row["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    if datetime.now(timezone.utc) - created > timedelta(days=ttl_days):
        return None
    data = json_load(row["results"], None)
    return data if isinstance(data, list) else None


def cache_put(
    conn: sqlite3.Connection, key: str, query: str, results: list[dict[str, Any]]
) -> None:
    conn.execute(
        """
        INSERT INTO search_cache (key, query, results, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            query = excluded.query,
            results = excluded.results,
            created_at = excluded.created_at
        """,
        (key, query, json.dumps(results, ensure_ascii=False), utcnow()),
    )
    conn.commit()
