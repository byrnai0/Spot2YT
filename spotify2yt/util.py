from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def extract_spotify_id(ref: str) -> str | None:
    """Pull a playlist id out of a URL, URI, or raw id."""
    ref = (ref or "").strip()
    for pattern in (
        r"open\.spotify\.com/playlist/([A-Za-z0-9]+)",
        r"spotify:playlist:([A-Za-z0-9]+)",
    ):
        m = re.search(pattern, ref)
        if m:
            return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{10,}", ref):
        return ref
    return None


def extract_youtube_id(ref: str) -> str | None:
    """Pull an 11-char video id out of a YouTube/YouTube Music URL."""
    ref = (ref or "").strip()
    for pattern in (
        r"(?:[?&]v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ):
        m = re.search(pattern, ref)
        if m:
            return m.group(1)
    return None


def slugify(text: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9._ -]+", "_", text or "playlist")
    slug = re.sub(r"\s+", " ", slug).strip().replace(" ", "-")
    return (slug[:max_len] or "playlist").strip("-._")


def chunks(items, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def fmt_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "?:??"
    seconds = int(round(float(seconds)))
    if seconds < 0:
        return "?:??"
    return f"{seconds // 60}:{seconds % 60:02d}"


def parse_duration(value) -> int | None:
    """Accept ints (seconds) or strings like '3:45' / '1:02:03'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        total = 0
        try:
            for part in text.split(":"):
                total = total * 60 + int(part)
            return total
        except ValueError:
            return None
    try:
        return int(float(text))
    except ValueError:
        return None


def json_load(text, default):
    import json

    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
