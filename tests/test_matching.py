from __future__ import annotations

import unittest

from spotify2yt.matching import (
    Candidate,
    TrackInfo,
    classify,
    clean_title,
    extract_version_tags,
    normalize,
    rank_candidates,
    score_candidate,
)


def make_track(title="Blinding Lights", artists=None, album="After Hours", ms=200_040):
    return TrackInfo(
        title=title,
        artists=artists if artists is not None else ["The Weeknd"],
        album=album,
        duration_ms=ms,
    )


def make_candidate(
    title="Blinding Lights",
    artists=None,
    duration=200,
    video_id="vid00000001",
    video_type="MUSIC_VIDEO_TYPE_ATV",
    album="After Hours",
):
    return Candidate(
        video_id=video_id,
        title=title,
        artists=artists if artists is not None else ["The Weeknd"],
        album=album,
        duration=duration,
        video_type=video_type,
        result_type="song",
    )


class TestNormalisation(unittest.TestCase):
    def test_accents_and_punctuation(self):
        self.assertEqual(normalize("Beyoncé"), "beyonce")
        self.assertEqual(normalize("AC/DC"), "ac dc")
        self.assertEqual(normalize("Don't Stop & Go"), "don t stop and go")

    def test_feat_is_removed(self):
        self.assertEqual(normalize("Song feat. Somebody"), "song")
        self.assertEqual(normalize("Song (ft. Somebody)"), "song")

    def test_clean_title_removes_release_noise(self):
        self.assertEqual(clean_title("Song (Remastered 2011)"), "song")
        self.assertEqual(clean_title("Song - Remastered"), "song")
        self.assertEqual(clean_title("Song (Official Video)"), "song")
        self.assertEqual(clean_title("Song (Live at Wembley)"), "song")

    def test_clean_title_keeps_real_words(self):
        self.assertEqual(clean_title("Live and Let Die"), "live and let die")
        self.assertEqual(clean_title("Song (Reprise)"), "song reprise")

    def test_version_tags_need_context(self):
        self.assertEqual(extract_version_tags("Live and Let Die"), set())
        self.assertIn("live", extract_version_tags("Song (Live at Wembley)"))
        self.assertIn("live", extract_version_tags("Song - Live"))
        self.assertIn("remix", extract_version_tags("Song (Somebody Remix)"))
        self.assertEqual(extract_version_tags("Song - Remastered 2011"), set())


class TestScoring(unittest.TestCase):
    def test_exact_match_is_auto(self):
        track = make_track()
        candidate = make_candidate()
        score, breakdown = score_candidate(track, candidate)
        self.assertGreater(score, 0.95)
        self.assertEqual(
            classify(score, breakdown, auto_threshold=0.80, review_threshold=0.50),
            "auto",
        )

    def test_karaoke_version_is_penalised(self):
        track = make_track()
        official = make_candidate()
        karaoke = make_candidate(
            title="Blinding Lights (Karaoke Version)",
            artists=["Karaoke Kings"],
            video_id="vid00000002",
            video_type="MUSIC_VIDEO_TYPE_UGC",
            album=None,
        )
        official_score, _ = score_candidate(track, official)
        karaoke_score, karaoke_breakdown = score_candidate(track, karaoke)
        self.assertLess(karaoke_score, official_score)
        self.assertTrue(karaoke_breakdown["hard_mismatch"])
        self.assertNotEqual(classify(karaoke_score, karaoke_breakdown), "auto")

    def test_live_version_goes_to_review(self):
        track = make_track(title="Blinding Lights (Live)")
        candidate = make_candidate()
        score, breakdown = score_candidate(track, candidate)
        self.assertTrue(breakdown["hard_mismatch"])
        self.assertEqual(classify(score, breakdown), "review")

    def test_wrong_duration_blocks_auto(self):
        track = make_track()
        candidate = make_candidate(duration=260)
        score, breakdown = score_candidate(track, candidate)
        self.assertLess(breakdown["duration"], 0.45)
        self.assertNotEqual(classify(score, breakdown), "auto")

    def test_wrong_artist_blocks_auto(self):
        track = make_track()
        candidate = make_candidate(artists=["Some Other Band"], album="Other")
        score, breakdown = score_candidate(track, candidate)
        self.assertLess(breakdown["artist"], 0.55)
        self.assertNotEqual(classify(score, breakdown), "auto")

    def test_similar_but_wrong_artist_and_jazz_version_is_review(self):
        track = make_track(title="Clair de lune", artists=["Claude Debussy"], ms=300_000)
        candidate = make_candidate(
            title="Clair de Lune (Jazz Nightclub Version)",
            artists=["Johann Debussy"],
            duration=300,
            album=None,
        )
        score, breakdown = score_candidate(track, candidate)
        self.assertIn("alternate", breakdown["yt_tags"])
        self.assertTrue(breakdown["hard_mismatch"])
        self.assertNotEqual(classify(score, breakdown), "auto")

    def test_remix_against_studio_goes_to_review(self):
        track = make_track(title="Blinding Lights (Remix)")
        candidate = make_candidate()
        score, breakdown = score_candidate(track, candidate)
        self.assertTrue(breakdown["hard_mismatch"])
        self.assertEqual(classify(score, breakdown), "review")

    def test_ranking_prefers_official_album_track(self):
        track = make_track()
        results = [
            {
                "videoId": "karaoke001",
                "title": "Blinding Lights (Karaoke)",
                "artists": [{"name": "Karaoke Kings"}],
                "album": None,
                "duration_seconds": 200,
                "videoType": "MUSIC_VIDEO_TYPE_UGC",
                "resultType": "video",
            },
            {
                "videoId": "official01",
                "title": "Blinding Lights",
                "artists": [{"name": "The Weeknd"}],
                "album": {"name": "After Hours"},
                "duration_seconds": 200,
                "videoType": "MUSIC_VIDEO_TYPE_ATV",
                "resultType": "song",
            },
        ]
        ranked = rank_candidates(track, results)
        self.assertEqual(ranked[0].video_id, "official01")
        self.assertGreater(ranked[0].score, ranked[1].score)


if __name__ == "__main__":
    unittest.main()
