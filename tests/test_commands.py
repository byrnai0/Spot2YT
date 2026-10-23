from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spotify2yt.commands import (
    apply_review,
    export_playlist,
    import_playlist,
    match_playlist,
    sync_playlist,
    write_review,
)
from spotify2yt.config import Settings
from spotify2yt.db import connect
from spotify2yt.store import list_local_playlists, status_counts


class FakeSpotify:
    def __init__(self):
        self.name = "Test Playlist"

    def playlist(self, playlist_id):
        return {
            "id": "37i9dQZF1DXcBWIGoYBM5M",
            "name": self.name,
            "description": "test",
            "owner": {"display_name": "me"},
            "snapshot_id": "snap",
            "tracks": {"total": 4},
            "external_urls": {"spotify": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"},
        }

    def current_user_playlists(self, limit=50):
        return {
            "items": [
                {
                    "id": "37i9dQZF1DXcBWIGoYBM5M",
                    "name": self.name,
                    "owner": {"display_name": "me"},
                    "tracks": {"total": 4},
                }
            ],
            "next": None,
        }

    def playlist_items(self, playlist_id, additional_types=("track",), limit=100):
        return {
            "items": [
                {
                    "added_at": "2024-01-01T00:00:00Z",
                    "track": {
                        "id": "s1",
                        "name": "Blinding Lights",
                        "type": "track",
                        "artists": [{"name": "The Weeknd"}],
                        "album": {"name": "After Hours"},
                        "duration_ms": 200_040,
                        "external_ids": {"isrc": "USUG11904206"},
                    },
                },
                {
                    "added_at": "2024-01-01T00:00:00Z",
                    "track": {
                        "id": "s2",
                        "name": "Karaoke Song",
                        "type": "track",
                        "artists": [{"name": "Some Artist"}],
                        "album": {"name": "Some Album"},
                        "duration_ms": 180_000,
                        "external_ids": {},
                    },
                },
                {
                    "added_at": "2024-01-01T00:00:00Z",
                    "track": None,
                },
                {
                    "added_at": "2024-01-01T00:00:00Z",
                    "is_local": True,
                    "track": {
                        "id": None,
                        "name": "Local File",
                        "type": "track",
                        "artists": [{"name": "Me"}],
                        "duration_ms": 1000,
                    },
                },
            ],
            "next": None,
        }


class FakeYTMusic:
    def __init__(self):
        self.added: list[str] = []
        self.created: list[dict] = []
        self.playlists: list[dict] = []

    def search(self, query, filter=None, limit=20):
        if "Blinding Lights" in query:
            return [
                {
                    "videoId": "vidKaraoke",
                    "title": "Blinding Lights (Karaoke Version)",
                    "artists": [{"name": "Karaoke Kings"}],
                    "album": None,
                    "duration_seconds": 200,
                    "videoType": "MUSIC_VIDEO_TYPE_UGC",
                    "resultType": "video",
                },
                {
                    "videoId": "vidBlinding",
                    "title": "Blinding Lights",
                    "artists": [{"name": "The Weeknd"}],
                    "album": {"name": "After Hours"},
                    "duration_seconds": 200,
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                    "resultType": "song",
                },
            ]
        return [
            {
                "videoId": "vidKaraoke",
                "title": "Karaoke Song (Karaoke Version)",
                "artists": [{"name": "Karaoke Kings"}],
                "album": None,
                "duration_seconds": 181,
                "videoType": "MUSIC_VIDEO_TYPE_UGC",
                "resultType": "video",
            }
        ]

    def create_playlist(self, title, description, privacy_status="PRIVATE"):
        self.created.append({"title": title, "privacy": privacy_status})
        self.playlists.append({"playlistId": "yt37i9dQZF1DXcBWIGoYBM5M", "title": title})
        return "yt37i9dQZF1DXcBWIGoYBM5M"

    def get_library_playlists(self, limit=None):
        return self.playlists

    def get_playlist(self, playlist_id, limit=None):
        return {"title": "My Playlist", "tracks": [{"videoId": v} for v in self.added]}

    def add_playlist_items(self, playlistId, videoIds=None, duplicates=False):
        self.added.extend(videoIds or [])
        return "STATUS_SUCCEEDED"

    def get_account_info(self):
        return {"accountName": "tester"}


class TestCommandFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(
            home=root,
            db_path=root / "state.db",
            exports_dir=root / "exports",
            spotify_client_id="client",
            spotify_client_secret=None,
            spotify_redirect_uri="http://127.0.0.1:8888/callback",
            spotify_cache_path=root / "auth" / "spotify.json",
            yt_auth_path=root / "auth" / "browser.json",
            delay=0,
            auto_threshold=0.80,
            review_threshold=0.50,
            search_limit=5,
            batch_size=2,
            cache_ttl_days=30,
            google_client_id=None,
            google_client_secret=None,
        )
        self.settings.exports_dir.mkdir(parents=True)
        self.conn = connect(self.settings.db_path)
        self.sp = FakeSpotify()
        self.yt = FakeYTMusic()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _patches(self):
        return (
            patch("spotify2yt.commands.make_spotify", return_value=self.sp),
            patch("spotify2yt.commands.make_ytmusic", return_value=self.yt),
        )

    def test_full_flow(self):
        sp_patch, yt_patch = self._patches()
        with sp_patch, yt_patch:
            meta = export_playlist(self.settings, self.conn, "Test Playlist")
            self.assertEqual(meta["id"], "37i9dQZF1DXcBWIGoYBM5M")
            counts = status_counts(self.conn, "37i9dQZF1DXcBWIGoYBM5M")
            self.assertIn("pending", counts)
            self.assertEqual(sum(counts.values()), 2)  # removed + local skipped

            match_playlist(self.settings, self.conn, "Test Playlist")
            counts = status_counts(self.conn, "37i9dQZF1DXcBWIGoYBM5M")
            self.assertEqual(counts.get("auto"), 1)
            self.assertIn("unmatched", counts)

            review_path = write_review(self.settings, self.conn, "Test Playlist")
            self.assertTrue(review_path.exists())

            with review_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "unmatched")
            rows[0]["choice"] = "1"
            with review_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)

            apply_review(self.settings, self.conn, "Test Playlist", str(review_path))
            counts = status_counts(self.conn, "37i9dQZF1DXcBWIGoYBM5M")
            self.assertEqual(counts.get("manual"), 1)

            # Dry run must not create or add anything.
            sync_playlist(
                self.settings, self.conn, "Test Playlist", dry_run=True
            )
            self.assertEqual(self.yt.added, [])
            self.assertEqual(self.yt.created, [])

            sync_playlist(self.settings, self.conn, "Test Playlist")
            self.assertEqual(len(self.yt.created), 1)
            self.assertIn("vidBlinding", self.yt.added)
            self.assertIn("vidKaraoke", self.yt.added)
            counts = status_counts(self.conn, "37i9dQZF1DXcBWIGoYBM5M")
            self.assertEqual(counts.get("added"), 2)

            row = self.conn.execute(
                "SELECT yt_playlist_id FROM playlists WHERE spotify_id = '37i9dQZF1DXcBWIGoYBM5M'"
            ).fetchone()
            self.assertEqual(row["yt_playlist_id"], "yt37i9dQZF1DXcBWIGoYBM5M")

            # Rerun is idempotent: reconciliation sees both videos.
            before = len(self.yt.added)
            sync_playlist(self.settings, self.conn, "Test Playlist")
            self.assertEqual(len(self.yt.added), before)

    def test_import_flow_without_spotify(self):
        csv_path = Path(self.tmp.name) / "export.csv"
        csv_path.write_text(
            "Track Name,Artist Name(s),Album Name,Track Duration (ms)\n"
            "Blinding Lights,The Weeknd,After Hours,200040\n"
            "Karaoke Song,Some Artist,Some Album,180000\n",
            encoding="utf-8",
        )
        _, yt_patch = self._patches()
        with yt_patch:
            import_playlist(self.settings, self.conn, str(csv_path), "Imported")
            playlists = list_local_playlists(self.conn)
            self.assertEqual(len(playlists), 1)
            self.assertEqual(playlists[0]["name"], "Imported")
            self.assertEqual(playlists[0]["track_count"], 2)

            match_playlist(self.settings, self.conn, "Imported")
            counts = status_counts(self.conn, playlists[0]["spotify_id"])
            self.assertEqual(counts.get("auto"), 1)

            sync_playlist(self.settings, self.conn, "Imported")
            self.assertIn("vidBlinding", self.yt.added)
            self.assertEqual(len(self.yt.created), 1)


if __name__ == "__main__":
    unittest.main()

