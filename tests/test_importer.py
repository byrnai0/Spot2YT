from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spotify2yt.errors import S2YError
from spotify2yt.importer import read_import


class TestImporter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exportify_csv(self):
        path = self.root / "playlist.csv"
        path.write_text(
            "\ufeffTrack URI,Track Name,Artist Name(s),Album Name,"
            "Track Duration (ms),ISRC,Added At\n"
            'spotify:track:abc123abc123abc123abc1,Blinding Lights,"The Weeknd",'
            "After Hours,200040,USUG11904206,2024-01-01\n"
            "spotify:track:def456def456def456def4,Song,Artist One;Artist Two,"
            "Album,180000,,2024-01-02\n"
            ",No URI But Fine,Someone,,3:45,,\n",
            encoding="utf-8",
        )
        name, tracks = read_import(path)
        self.assertEqual(name, "playlist")
        self.assertEqual(len(tracks), 3)
        self.assertEqual(tracks[0]["title"], "Blinding Lights")
        self.assertEqual(tracks[0]["artists"], ["The Weeknd"])
        self.assertEqual(tracks[0]["duration_ms"], 200_040)
        self.assertEqual(tracks[0]["spotify_id"], "abc123abc123abc123abc1")
        self.assertEqual(tracks[1]["artists"], ["Artist One", "Artist Two"])
        self.assertTrue(tracks[2]["spotify_id"].startswith("import:"))
        self.assertEqual(tracks[2]["duration_ms"], 225_000)

    def test_json_list(self):
        path = self.root / "tracks.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "trackName": "Blinding Lights",
                        "artistName": "The Weeknd",
                        "albumName": "After Hours",
                        "trackUri": "spotify:track:abc123abc123abc123abc1",
                        "duration_ms": 200040,
                    }
                ]
            ),
            encoding="utf-8",
        )
        name, tracks = read_import(path, "My Import")
        self.assertEqual(name, "My Import")
        self.assertEqual(tracks[0]["duration_ms"], 200_040)
        self.assertEqual(tracks[0]["spotify_id"], "abc123abc123abc123abc1")

    def test_json_playlists_requires_name_when_ambiguous(self):
        path = self.root / "data.json"
        path.write_text(
            json.dumps(
                {
                    "playlists": [
                        {"name": "One", "tracks": [{"track": {"name": "A", "artists": ["X"]}}]},
                        {"name": "Two", "tracks": [{"track": {"name": "B", "artists": ["Y"]}}]},
                    ]
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(S2YError):
            read_import(path)
        name, tracks = read_import(path, "Two")
        self.assertEqual(name, "Two")
        self.assertEqual(tracks[0]["title"], "B")

    def test_missing_title_column(self):
        path = self.root / "bad.csv"
        path.write_text("Artist,Album\nX,Y\n", encoding="utf-8")
        with self.assertRaises(S2YError):
            read_import(path)

    def test_empty_file(self):
        path = self.root / "empty.csv"
        path.write_text("Track Name,Artist Name(s)\n", encoding="utf-8")
        with self.assertRaises(S2YError):
            read_import(path)


if __name__ == "__main__":
    unittest.main()
