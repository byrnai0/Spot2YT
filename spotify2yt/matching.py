from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from .util import fmt_duration, parse_duration


@dataclass
class TrackInfo:
    title: str
    artists: list[str]
    album: str | None = None
    duration_ms: int | None = None


@dataclass
class Candidate:
    video_id: str
    title: str
    artists: list[str]
    album: str | None
    duration: int | None
    video_type: str | None = None
    result_type: str | None = None
    score: float = 0.0
    breakdown: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Candidate | None":
        video_id = raw.get("videoId") or raw.get("video_id")
        if not video_id:
            return None
        artists: list[str] = []
        for artist in raw.get("artists") or []:
            if isinstance(artist, dict):
                name = artist.get("name")
            else:
                name = str(artist)
            if name:
                artists.append(str(name))
        album = raw.get("album")
        if isinstance(album, dict):
            album = album.get("name")
        duration = raw.get("duration_seconds")
        if duration is None:
            duration = parse_duration(raw.get("duration"))
        return cls(
            video_id=str(video_id),
            title=str(raw.get("title") or ""),
            artists=artists,
            album=str(album) if album else None,
            duration=int(duration) if duration is not None else None,
            video_type=raw.get("videoType"),
            result_type=raw.get("resultType"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "artists": self.artists,
            "album": self.album,
            "duration": self.duration,
            "video_type": self.video_type,
            "result_type": self.result_type,
            "score": round(self.score, 4),
        }

    def display(self) -> str:
        artists = ", ".join(self.artists) if self.artists else "?"
        album = f" - {self.album}" if self.album else ""
        return (
            f"{self.title} - {artists}{album} "
            f"({fmt_duration(self.duration)}) [{self.video_id}] "
            f"score={self.score:.2f}"
        )


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

VERSION_PATTERNS = {
    "live": re.compile(r"\b(live|unplugged|in concert|concert version)\b", re.I),
    "remix": re.compile(r"\b(remix|rmx|bootleg|flip|mashup|rework)\b", re.I),
    "acoustic": re.compile(r"\bacoustic\b", re.I),
    "instrumental": re.compile(r"\b(instrumental|inst\.?)\b", re.I),
    "karaoke": re.compile(r"\bkaraoke\b", re.I),
    "cover": re.compile(r"\b(cover|tribute)\b", re.I),
    "spedup": re.compile(r"\b(sped\s*up|speed\s*up|nightcore|8d)\b", re.I),
    "alternate": re.compile(
        r"\b(jazz|nightclub|night club|lofi|lo-fi|piano|orchestral|sax|"
        r"saxophone|a cappella|acappella|music box|8-bit|8 bit|rockabye)\b",
        re.I,
    ),
    "demo": re.compile(r"\bdemo\b", re.I),
    "extended": re.compile(r"\b(extended|club mix)\b", re.I),
}

VERSION_ANY = re.compile(
    "|".join(f"(?:{p.pattern})" for p in VERSION_PATTERNS.values()), re.I
)
BENIGN = re.compile(
    r"\b(remaster(?:ed)?|deluxe|bonus|explicit|clean|official|lyrics?|"
    r"visuali[sz]er|audio|video|hd|4k|from the .* soundtrack)\b",
    re.I,
)


def normalize(text: str | None) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"\b(feat|ft|featuring)\b.*$", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def extract_version_tags(title: str | None) -> set[str]:
    """Detect (Live)/(Remix)/- Live at ... without flagging 'Live and Let Die'."""
    title = title or ""
    contexts = re.findall(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]", title)
    dash_parts = re.split(r"\s+-\s+", title)
    if len(dash_parts) > 1:
        contexts.extend(dash_parts[1:])
    blob = " ".join(contexts)
    return {tag for tag, rx in VERSION_PATTERNS.items() if rx.search(blob)}


def clean_title(title: str | None) -> str:
    """Remove version/quality noise, then normalise for fuzzy comparison."""
    title = title or ""

    def repl(match: re.Match[str]) -> str:
        inner = match.group(1)
        return " " if (VERSION_ANY.search(inner) or BENIGN.search(inner)) else match.group(0)

    text = re.sub(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]", repl, title)
    parts = re.split(r"\s+-\s+", text)
    if len(parts) > 1 and (VERSION_ANY.search(parts[-1]) or BENIGN.search(parts[-1])):
        parts = parts[:-1]
    return normalize(" - ".join(parts))


def build_query(track: TrackInfo) -> str:
    title = re.sub(
        r"\s*[\(\[][^\)\]]*(remaster(?:ed)?|deluxe|bonus|explicit|clean)[^\)\]]*[\)\]]",
        "",
        track.title,
        flags=re.I,
    )
    title = re.sub(
        r"\s+-\s+(remaster(?:ed)?|deluxe|bonus|explicit|clean)\b.*$",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s+", " ", title).strip()
    artist = track.artists[0] if track.artists else ""
    return f"{title} {artist}".strip()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return max(
        fuzz.ratio(a, b),
        fuzz.token_sort_ratio(a, b),
        fuzz.token_set_ratio(a, b),
    ) / 100.0


def artist_similarity(spotify_artists: list[str], yt_artists: list[str]) -> float:
    if not yt_artists:
        return 0.20
    best = 0.0
    for a in spotify_artists or [""]:
        for b in yt_artists:
            best = max(best, fuzz.token_set_ratio(normalize(a), normalize(b)) / 100.0)
    joined_a = normalize(" ".join(spotify_artists))
    joined_b = normalize(" ".join(yt_artists))
    if joined_a and joined_b:
        best = max(best, fuzz.token_set_ratio(joined_a, joined_b) / 100.0)
    return best


def duration_similarity(spotify_ms: int | None, yt_seconds: int | None) -> float:
    if not spotify_ms or yt_seconds is None:
        return 0.5
    delta = abs((spotify_ms / 1000.0) - yt_seconds)
    if delta <= 2:
        return 1.0
    if delta >= 30:
        return 0.0
    return 1.0 - (delta - 2) / 28.0


def score_candidate(track: TrackInfo, candidate: Candidate) -> tuple[float, dict[str, Any]]:
    sp_title = clean_title(track.title)
    yt_title = clean_title(candidate.title)
    title = title_similarity(sp_title, yt_title)
    artist = artist_similarity(track.artists, candidate.artists)
    duration = duration_similarity(track.duration_ms, candidate.duration)

    sp_tags = extract_version_tags(track.title)
    yt_tags = extract_version_tags(candidate.title)
    hard_mismatch = False
    penalty = 0.0

    # Karaoke/cover/sped-up versions are almost never what the user wants.
    for tag, weight in (
        ("karaoke", 0.50),
        ("cover", 0.40),
        ("spedup", 0.40),
        ("alternate", 0.30),
        ("instrumental", 0.30),
    ):
        if tag in yt_tags and tag not in sp_tags:
            penalty += weight
            hard_mismatch = True

    # A live/remix/acoustic version is a different recording; send to review.
    for tag in ("live", "remix", "acoustic", "demo", "extended"):
        if tag in yt_tags and tag not in sp_tags:
            penalty += 0.15
            if tag in ("live", "remix"):
                hard_mismatch = True
        elif tag in sp_tags and tag not in yt_tags:
            penalty += 0.12
            if tag in ("live", "remix"):
                hard_mismatch = True

    yt_artists_norm = normalize(" ".join(candidate.artists))
    if any(w in yt_artists_norm for w in ("karaoke", "tribute", "cover band")):
        if not any(w in normalize(track.title) for w in ("karaoke", "tribute")):
            penalty += 0.35
            hard_mismatch = True
    if "karaoke" in normalize(candidate.title) and "karaoke" not in normalize(track.title):
        penalty += 0.35
        hard_mismatch = True

    bonus = 0.0
    if candidate.video_type == "MUSIC_VIDEO_TYPE_ATV":
        bonus += 0.03  # official album track
    if (
        candidate.album
        and track.album
        and fuzz.token_set_ratio(normalize(track.album), normalize(candidate.album)) >= 85
    ):
        bonus += 0.02
    if candidate.result_type == "song":
        bonus += 0.01

    total = 0.55 * title + 0.30 * artist + 0.15 * duration + bonus - penalty
    total = max(0.0, min(1.0, total))

    breakdown = {
        "title": round(title, 4),
        "artist": round(artist, 4),
        "duration": round(duration, 4),
        "bonus": round(bonus, 4),
        "penalty": round(penalty, 4),
        "hard_mismatch": hard_mismatch,
        "spotify_tags": sorted(sp_tags),
        "yt_tags": sorted(yt_tags),
    }
    return total, breakdown


def classify(
    score: float,
    breakdown: dict[str, Any],
    auto_threshold: float = 0.80,
    review_threshold: float = 0.50,
) -> str:
    if (
        score >= auto_threshold
        and breakdown["title"] >= 0.82
        and breakdown["artist"] >= 0.70
        and breakdown["duration"] >= 0.45
        and not breakdown["hard_mismatch"]
    ):
        return "auto"
    return "review" if score >= review_threshold else "unmatched"


def rank_candidates(
    track: TrackInfo, raw_results: list[dict[str, Any]], limit: int = 5
) -> list[Candidate]:
    seen: set[str] = set()
    candidates: list[Candidate] = []
    for raw in raw_results or []:
        candidate = Candidate.from_raw(raw)
        if candidate is None or candidate.video_id in seen:
            continue
        seen.add(candidate.video_id)
        candidate.score, candidate.breakdown = score_candidate(track, candidate)
        candidates.append(candidate)
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:limit]

