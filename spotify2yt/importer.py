from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .errors import S2YError
from .util import parse_duration

TITLE_ALIASES = ("track name", "title", "song", "name", "track")
ARTIST_ALIASES = (
    "artist name(s)",
    "artist name",
    "artist(s)",
    "artists",
    "artist",
)
ALBUM_ALIASES = ("album name", "album")
DURATION_MS_ALIASES = (
    "track duration (ms)",
    "duration (ms)",
    "duration_ms",
    "durationms",
    "length (ms)",
)
DURATION_ALIASES = ("duration", "length", "track duration")
URI_ALIASES = (
    "track uri",
    "spotify id",
    "spotify track id",
    "track id",
    "uri",
    "id",
)
ISRC_ALIASES = ("isrc",)
ADDED_AT_ALIASES = ("added at", "added_at", "date added")


def _split_artists(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("name")
            if item:
                result.append(str(item).strip())
        return [item for item in result if item]
    text = str(value).strip()
    if not text:
        return []
    # Exportify / TuneMyMusic use ";" or "|" between artists. Do not split on
    # commas: "Earth, Wind & Fire" is one artist.
    for sep in (";", "|"):
        if sep in text:
            return [part.strip() for part in text.split(sep) if part.strip()]
    return [text]


def track_spotify_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in (
        r"spotify:track:([A-Za-z0-9]+)",
        r"open\.spotify\.com/track/([A-Za-z0-9]+)",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{10,}", text):
        return text
    return None


def _synthetic_id(title: str, artists: list[str]) -> str:
    digest = hashlib.sha1(
        f"{title.casefold()}|{'|'.join(a.casefold() for a in artists)}".encode("utf-8")
    ).hexdigest()[:16]
    return f"import:{digest}"


def _duration_to_ms(value: Any, assumed_ms: bool) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if ":" in text:
        seconds = parse_duration(text)
        return int(seconds * 1000) if seconds is not None else None
    try:
        number = float(text)
    except ValueError:
        return None
    if assumed_ms or number >= 10_000:
        return int(number)
    return int(number * 1000)


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def _read_csv(path: Path, name: str | None) -> tuple[str, list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        if not reader.fieldnames:
            raise S2YError(f"{path} has no header row.")
        fields = {_norm(header): header for header in reader.fieldnames if header}

        def pick(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
            for alias in aliases:
                header = fields.get(alias)
                if header is not None and row.get(header) not in (None, ""):
                    return row.get(header)
            return None

        if not any(alias in fields for alias in TITLE_ALIASES):
            raise S2YError(
                "CSV needs a title column. Supported names include "
                "'Track Name', 'Title', or 'Song'."
            )

        tracks: list[dict[str, Any]] = []
        position = 0
        for row in reader:
            title = str(pick(row, TITLE_ALIASES) or "").strip()
            if not title:
                continue
            artists = _split_artists(pick(row, ARTIST_ALIASES))
            if not artists:
                artists = ["Unknown Artist"]
            raw_uri = pick(row, URI_ALIASES)
            spotify_id = track_spotify_id(raw_uri) or _synthetic_id(title, artists)
            duration = _duration_to_ms(pick(row, DURATION_MS_ALIASES), assumed_ms=True)
            if duration is None:
                duration = _duration_to_ms(pick(row, DURATION_ALIASES), assumed_ms=False)
            tracks.append(
                {
                    "position": position,
                    "spotify_id": spotify_id,
                    "title": title,
                    "artists": artists,
                    "album": str(pick(row, ALBUM_ALIASES) or "").strip() or None,
                    "duration_ms": duration,
                    "isrc": str(pick(row, ISRC_ALIASES) or "").strip() or None,
                    "explicit": 0,
                    "is_local": 0,
                    "added_at": str(pick(row, ADDED_AT_ALIASES) or "").strip() or None,
                }
            )
            position += 1

    return (name or path.stem), tracks


def _json_track(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("track"), dict):
        item = item["track"]

    title = item.get("trackName") or item.get("name") or item.get("title")
    if not title:
        return None
    artists = _split_artists(
        item.get("artistName") or item.get("artists") or item.get("artist")
    )
    if not artists:
        artists = ["Unknown Artist"]
    album = item.get("albumName") or item.get("album")
    if isinstance(album, dict):
        album = album.get("name")
    uri = (
        item.get("trackUri")
        or item.get("uri")
        or item.get("spotify_id")
        or item.get("id")
    )
    duration = (
        item.get("duration_ms")
        or item.get("durationMs")
        or item.get("duration")
        or item.get("trackDuration")
    )
    duration_is_ms = bool(item.get("duration_ms") or item.get("durationMs"))
    return {
        "spotify_id": track_spotify_id(uri) or _synthetic_id(str(title), artists),
        "title": str(title).strip(),
        "artists": artists,
        "album": str(album).strip() if album else None,
        "duration_ms": _duration_to_ms(duration, assumed_ms=duration_is_ms),
        "isrc": item.get("isrc"),
        "explicit": 1 if item.get("explicit") else 0,
        "is_local": 0,
        "added_at": item.get("addedAt") or item.get("added_at"),
    }


def _read_json(path: Path, name: str | None) -> tuple[str, list[dict[str, Any]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise S2YError(f"Could not parse {path}: {exc}") from exc

    playlist_name = name
    raw_tracks: Any

    if isinstance(data, dict) and isinstance(data.get("playlists"), list):
        playlists = data["playlists"]
        if not playlists:
            raise S2YError("JSON contains an empty 'playlists' list.")
        if name:
            wanted = name.casefold()
            playlist = next(
                (p for p in playlists if str(p.get("name") or "").casefold() == wanted),
                None,
            )
            if playlist is None:
                names = ", ".join(str(p.get("name")) for p in playlists[:10])
                raise S2YError(f"No playlist named {name!r}. Available: {names}")
        elif len(playlists) == 1:
            playlist = playlists[0]
        else:
            names = ", ".join(str(p.get("name")) for p in playlists[:10])
            raise S2YError(
                "This JSON contains multiple playlists. Pick one with --name. "
                f"Available: {names}"
            )
        playlist_name = name or playlist.get("name") or path.stem
        raw_tracks = playlist.get("tracks") or playlist.get("items") or []
    elif isinstance(data, list):
        raw_tracks = data
    elif isinstance(data, dict):
        playlist_name = name or data.get("name") or data.get("title") or path.stem
        raw_tracks = data.get("tracks") or data.get("items") or []
    else:
        raise S2YError("JSON must be a list of tracks or an object with 'tracks'.")

    tracks: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_tracks):
        track = _json_track(raw)
        if track is None:
            continue
        track["position"] = position
        tracks.append(track)
    return str(playlist_name), tracks


def read_import(path: Path, name: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    path = Path(path).expanduser()
    if not path.exists():
        raise S2YError(f"Import file not found: {path}")
    if path.suffix.lower() == ".json":
        playlist_name, tracks = _read_json(path, name)
    else:
        playlist_name, tracks = _read_csv(path, name)
    if not tracks:
        raise S2YError(f"No tracks found in {path}.")
    return playlist_name, tracks
