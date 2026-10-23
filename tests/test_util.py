from __future__ import annotations

import unittest

from spotify2yt.matching import TrackInfo, build_query
from spotify2yt.util import extract_spotify_id, extract_youtube_id, fmt_duration


class TestRefs(unittest.TestCase):
    def test_spotify_url(self):
        self.assertEqual(
            extract_spotify_id(
                "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc"
            ),
            "37i9dQZF1DXcBWIGoYBM5M",
        )

    def test_spotify_uri_and_raw(self):
        self.assertEqual(
            extract_spotify_id("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"),
            "37i9dQZF1DXcBWIGoYBM5M",
        )
        self.assertEqual(extract_spotify_id("37i9dQZF1DXcBWIGoYBM5M"), "37i9dQZF1DXcBWIGoYBM5M")
        self.assertIsNone(extract_spotify_id("my playlist name"))

    def test_youtube_urls(self):
        self.assertEqual(
            extract_youtube_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAM"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(
            extract_youtube_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ"
        )
        self.assertEqual(extract_youtube_id("dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertIsNone(extract_youtube_id("not a video"))

    def test_query_strips_remaster(self):
        track = TrackInfo("Song - Remastered 2011", ["Artist"], duration_ms=1000)
        self.assertEqual(build_query(track), "Song Artist")

    def test_duration(self):
        self.assertEqual(fmt_duration(200), "3:20")
        self.assertEqual(fmt_duration(None), "?:??")


if __name__ == "__main__":
    unittest.main()
