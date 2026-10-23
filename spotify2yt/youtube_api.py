from __future__ import annotations

import json
import random
import time
from typing import Any, Callable

import requests
import ytmusicapi
from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicGatedError, YTMusicServerError, YTMusicUserError

from .config import Settings
from .errors import AuthError, S2YError
from .ui import info


def make_ytmusic(settings: Settings, required: bool = True) -> YTMusic:
    path = settings.yt_auth_path
    if not path.exists():
        if not required:
            try:
                return YTMusic()
            except Exception as exc:
                raise AuthError(f"Could not start an anonymous YouTube Music client: {exc}") from exc
        raise AuthError(
            "YouTube Music is not authenticated. Run `s2y yt-login` first "
            "(browser headers are the quickest route)."
        )
    try:
        return YTMusic(str(path))
    except Exception as exc:
        raise AuthError(
            f"Could not load YouTube Music auth from {path}. "
            "It may be stale or malformed; rerun `s2y yt-login`.\n\n"
            f"{exc}"
        ) from exc


def auth_kind(path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "unknown"
    if not isinstance(data, dict):
        return "unknown"
    if any(k in data for k in ("access_token", "refresh_token", "token")):
        return "oauth"
    if "cookie" in data:
        return "browser"
    return "unknown"


def is_auth_error(exc: Exception) -> bool:
    if isinstance(exc, YTMusicGatedError):
        return False
    message = str(exc).lower()
    return any(
        token in message
        for token in ("401", "403", "unauthorized", "forbidden", "cookie", "auth")
    )


def is_not_found(exc: Exception) -> bool:
    message = str(exc).lower()
    return "404" in message or "not found" in message or "does not exist" in message


def is_success(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, str):
        return "SUCCEEDED" in result.upper()
    if isinstance(result, dict):
        status = result.get("status") or result.get("STATUS") or ""
        return "SUCCEEDED" in str(status).upper()
    return bool(result)


class YTClient:
    """Throttled, retrying wrapper around ytmusicapi."""

    def __init__(self, yt: YTMusic, delay: float = 1.0, retries: int = 5):
        self.yt = yt
        self.delay = max(0.0, delay)
        self.retries = max(0, retries)
        self._last_call = 0.0

    # -- low level ---------------------------------------------------------
    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self.delay - (now - self._last_call)
        if wait > 0:
            time.sleep(wait + random.uniform(0.0, 0.25))
        self._last_call = time.monotonic()

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, YTMusicGatedError):
            return False
        if isinstance(exc, (YTMusicUserError,)):
            return False
        if isinstance(exc, YTMusicServerError):
            message = str(exc)
            if "429" in message:
                return True
            if any(code in message for code in ("400", "401", "403", "404")):
                return False
            return True
        if isinstance(exc, requests.HTTPError):
            status = exc.response.status_code if exc.response is not None else None
            return status in (408, 425, 429, 500, 502, 503, 504)
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "429",
                "too many requests",
                "timed out",
                "timeout",
                "connection reset",
                "temporarily unavailable",
                "502",
                "503",
                "504",
            )
        )

    @staticmethod
    def _retry_after(exc: Exception) -> float:
        response = getattr(exc, "response", None)
        if response is None:
            return 0.0
        raw = response.headers.get("Retry-After") if response.headers else None
        try:
            return float(raw) if raw else 0.0
        except (TypeError, ValueError):
            return 0.0

    def call(self, func: Callable, *args, **kwargs):
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                if attempt >= self.retries or not self._retryable(exc):
                    raise
                wait = min(90.0, 2 ** attempt * 2) + random.uniform(0.0, 1.5)
                wait = max(wait, self._retry_after(exc))
                info(f"YouTube Music hiccup ({type(exc).__name__}); retrying in {wait:.0f}s")
                time.sleep(wait)
        raise AssertionError("unreachable")

    # -- operations -------------------------------------------------------
    def search(self, query: str, filter_name: str | None = "songs", limit: int = 8):
        results = self.call(self.yt.search, query, filter=filter_name, limit=limit) or []
        # ytmusicapi can return a full shelf even when a smaller limit is passed.
        return results[:limit] if limit else results

    def get_account_info(self) -> dict[str, Any]:
        return self.call(self.yt.get_account_info) or {}

    def create_playlist(
        self, title: str, description: str, privacy: str = "PRIVATE"
    ) -> str:
        result = self.call(
            self.yt.create_playlist,
            title,
            description,
            privacy_status=privacy,
        )
        if isinstance(result, str) and result:
            return result
        if isinstance(result, dict):
            playlist_id = result.get("playlistId") or result.get("id")
            if playlist_id:
                return str(playlist_id)
        raise S2YError(f"YouTube Music did not create the playlist: {result}")

    def add_items(self, playlist_id: str, video_ids: list[str]) -> Any:
        result = self.call(
            self.yt.add_playlist_items,
            playlist_id,
            videoIds=video_ids,
            duplicates=False,
        )
        if not is_success(result):
            raise S2YError(f"YouTube Music rejected the batch: {result}")
        return result

    def playlist_video_ids(self, playlist_id: str) -> set[str]:
        playlist = self.call(self.yt.get_playlist, playlist_id, limit=None) or {}
        return {
            str(track["videoId"])
            for track in playlist.get("tracks") or []
            if track.get("videoId")
        }

    def get_playlist_summary(self, playlist_id: str) -> dict[str, Any]:
        return self.call(self.yt.get_playlist, playlist_id, limit=1) or {}

    def find_library_playlist(self, name: str) -> dict[str, Any] | None:
        playlists = self.call(self.yt.get_library_playlists, limit=None) or []
        wanted = name.casefold()
        for playlist in playlists:
            if (playlist.get("title") or "").casefold() == wanted:
                return playlist
        return None

    def library_playlists(self) -> list[dict[str, Any]]:
        return self.call(self.yt.get_library_playlists, limit=None) or []
