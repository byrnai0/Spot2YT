from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spotify2yt.db import connect
from spotify2yt.matching import Candidate
from spotify2yt.store import (
    cache_get,
    cache_key,
    cache_put,
    candidates_for_track,
    replace_tracks,
    resolve_local_playlist,
    save_match,
    status_counts,
    upsert_playlist,
)


def track(position, spotify_id="s1", title="Song"):
    return {
        "position": position,
        "spotify_id": spotify_id,
        "title": title,
        "artists": ["Artist"],
        "album": "Album",
        "duration_ms": 200_000,
        "isrc": None,
        "explicit": 0,
        "is_local": 0,
        "added_at": None,
    }


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "test.db")
        upsert_playlist(
            self.conn,
            {"id": "pl1", "name": "My Playlist", "owner": "me", "total": 2},
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_reexport_preserves_match(self):
        replace_tracks(self.conn, "pl1", [track(0)])
        candidate = Candidate(
            video_id="vid00000001",
            title="Song",
            artists=["Artist"],
            album="Album",
            duration=200,
            score=1.0,
        )
        save_match(self.conn, 1, "auto", [candidate], candidate)
        self.assertEqual(status_counts(self.conn, "pl1"), {"auto": 1})

        replace_tracks(self.conn, "pl1", [track(0)])
        self.assertEqual(status_counts(self.conn, "pl1"), {"auto": 1})
        self.assertEqual(
            candidates_for_track(self.conn, "pl1", 0)[0]["video_id"], "vid00000001"
        )

    def test_changed_song_resets_match(self):
        replace_tracks(self.conn, "pl1", [track(0)])
        candidate = Candidate(
            video_id="vid00000001",
            title="Song",
            artists=["Artist"],
            album="Album",
            duration=200,
            score=1.0,
        )
        save_match(self.conn, 1, "auto", [candidate], candidate)
        replace_tracks(self.conn, "pl1", [track(0, spotify_id="different")])
        self.assertEqual(status_counts(self.conn, "pl1"), {"pending": 1})

    def test_removed_tracks_are_deleted(self):
        replace_tracks(self.conn, "pl1", [track(0), track(1, spotify_id="s2")])
        self.assertEqual(status_counts(self.conn, "pl1"), {"pending": 2})
        replace_tracks(self.conn, "pl1", [track(0)])
        self.assertEqual(status_counts(self.conn, "pl1"), {"pending": 1})

    def test_resolve_by_name_and_id(self):
        self.assertEqual(resolve_local_playlist(self.conn, "My Playlist")["spotify_id"], "pl1")
        self.assertEqual(resolve_local_playlist(self.conn, "pl1")["spotify_id"], "pl1")

    def test_cache_roundtrip_and_expiry(self):
        results = [{"videoId": "x", "title": "Song"}]
        key = cache_key("Song Artist", "songs")
        cache_put(self.conn, key, "Song Artist", results)
        self.assertEqual(cache_get(self.conn, key, 30), results)
        self.assertIsNone(cache_get(self.conn, key, 0))


if __name__ == "__main__":
    unittest.main()
