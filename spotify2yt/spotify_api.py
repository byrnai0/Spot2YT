from __future__ import annotations

import time
from typing import Any, Iterator

import spotipy
from rapidfuzz import fuzz, process
from spotipy.oauth2 import SpotifyPKCE

from .config import Settings
from .errors import AuthError, PlaylistError, S2YError
from .util import extract_spotify_id

SCOPE = "playlist-read-private playlist-read-collaborative user-read-private"


def make_spotify(settings: Settings, open_browser: bool = True) -> spotipy.Spotify:
    if not settings.spotify_client_id:
        raise AuthError(
            "SPOTIFY_CLIENT_ID is not set. Create a free app at "
            "https://developer.spotify.com/dashboard, add the redirect URI "
            f"{settings.spotify_redirect_uri}, then put the Client ID in .env."
        )

    settings.spotify_cache_path.parent.mkdir(parents=True, exist_ok=True)
    auth_manager = SpotifyPKCE(
        client_id=settings.spotify_client_id,
        redirect_uri=settings.spotify_redirect_uri,
        scope=SCOPE,
        cache_path=str(settings.spotify_cache_path),
        open_browser=open_browser,
    )
    sp = spotipy.Spotify(
        auth_manager=auth_manager,
        requests_timeout=30,
        retries=5,
        backoff_factor=0.5,
    )
    try:
        sp.current_user()
    except spotipy.SpotifyException as exc:
        raise AuthError(
            "Spotify login failed. Check that the Redirect URI in your Spotify "
            f"app is exactly {settings.spotify_redirect_uri}.\n\n{exc}"
        ) from exc
    except Exception as exc:  # network/DNS
        raise AuthError(f"Could not reach Spotify: {exc}") from exc
    return sp


def current_user_name(sp: spotipy.Spotify) -> str:
    user = sp.current_user() or {}
    return user.get("display_name") or user.get("id") or "unknown"


def probe_spotify(settings: Settings) -> dict[str, Any] | None:
    """Verify a cached Spotify token without ever opening a browser/prompt."""
    if not settings.spotify_client_id:
        raise AuthError("SPOTIFY_CLIENT_ID is not set.")
    if not settings.spotify_cache_path.exists():
        return None

    auth_manager = SpotifyPKCE(
        client_id=settings.spotify_client_id,
        redirect_uri=settings.spotify_redirect_uri,
        scope=SCOPE,
        cache_path=str(settings.spotify_cache_path),
        open_browser=False,
    )
    token = auth_manager.get_cached_token()
    if not token:
        return None
    if float(token.get("expires_at") or 0) <= time.time() + 30:
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise AuthError("Spotify token expired and has no refresh token. Run `s2y spotify-login`.")
        token = auth_manager.refresh_access_token(refresh_token)
    sp = spotipy.Spotify(
        auth=token["access_token"], requests_timeout=30, retries=3
    )
    return sp.current_user() or {}


def list_user_playlists(sp: spotipy.Spotify) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        results = sp.current_user_playlists(limit=50)
    except spotipy.SpotifyException as exc:
        raise S2YError(f"Could not list Spotify playlists: {exc}") from exc

    while results:
        for item in results.get("items") or []:
            if not item or not item.get("id"):
                continue
            total_obj = item.get("tracks") or item.get("items") or {}
            rows.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name") or "(untitled)",
                    "owner": (item.get("owner") or {}).get("display_name"),
                    "total": total_obj.get("total") if isinstance(total_obj, dict) else None,
                }
            )
        if results.get("next"):
            results = sp.next(results)
        else:
            break
    return rows


def _meta_from_playlist(meta: dict[str, Any]) -> dict[str, Any]:
    total_obj = meta.get("tracks") or meta.get("items") or {}
    return {
        "id": meta.get("id"),
        "name": meta.get("name") or "(untitled)",
        "owner": (meta.get("owner") or {}).get("display_name"),
        "description": meta.get("description"),
        "snapshot_id": meta.get("snapshot_id"),
        "total": total_obj.get("total") if isinstance(total_obj, dict) else None,
        "url": (meta.get("external_urls") or {}).get("spotify"),
    }


def resolve_playlist(sp: spotipy.Spotify, ref: str) -> dict[str, Any]:
    ref = (ref or "").strip()
    if not ref:
        raise PlaylistError("No playlist given.")

    playlist_id = extract_spotify_id(ref)
    if playlist_id:
        try:
            meta = sp.playlist(playlist_id)
        except spotipy.SpotifyException as exc:
            raise PlaylistError(
                f"Could not open Spotify playlist {playlist_id}. "
                "Is it private, deleted, or owned by another account?\n\n"
                f"{exc}"
            ) from exc
        return _meta_from_playlist(meta)

    rows = list_user_playlists(sp)
    if not rows:
        raise PlaylistError("This Spotify account has no playlists.")

    exact = [r for r in rows if (r.get("name") or "").casefold() == ref.casefold()]
    if len(exact) == 1:
        return resolve_playlist(sp, exact[0]["id"])
    if len(exact) > 1:
        listing = "\n".join(f"  {r['id']}  {r['name']}" for r in exact)
        raise PlaylistError(
            f"{len(exact)} playlists are named {ref!r}. Use the ID instead:\n{listing}"
        )

    names = [r["name"] for r in rows]
    matches = process.extract(ref, names, scorer=fuzz.WRatio, limit=5)
    if matches and matches[0][1] >= 80:
        best_name = matches[0][0]
        best = next(r for r in rows if r["name"] == best_name)
        return resolve_playlist(sp, best["id"])

    listing = "\n".join(f"  {r['id']}  {r['name']}" for r in rows[:30])
    more = "" if len(rows) <= 30 else f"\n  ... and {len(rows) - 30} more"
    raise PlaylistError(
        f"No playlist matched {ref!r}. Run `s2y playlists` to see them.\n\n{listing}{more}"
    )


def iter_playlist_items(
    sp: spotipy.Spotify, playlist_id: str
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield (position, playlist item) for every item, including unavailable ones."""
    try:
        results = sp.playlist_items(
            playlist_id, additional_types=("track",), limit=100
        )
        position = 0
        while True:
            for item in results.get("items") or []:
                yield position, item
                position += 1
            if results.get("next"):
                results = sp.next(results)
            else:
                break
    except spotipy.SpotifyException as exc:
        raise S2YError(
            f"Spotify stopped returning playlist items: {exc}\n"
            "Wait a minute and rerun the command; Spotify rate-limits large reads."
        ) from exc


def item_to_track(item: dict[str, Any], position: int) -> tuple[dict[str, Any] | None, str]:
    """Convert one playlist item. Returns (track, skip_reason)."""
    track = item.get("track") or item.get("item")
    if not track:
        return None, "removed"
    if item.get("is_local") or track.get("is_local"):
        return None, "local"
    if track.get("type") and track.get("type") != "track":
        return None, "non_track"
    spotify_id = track.get("id")
    title = track.get("name")
    if not spotify_id or not title:
        return None, "removed"
    artists = [
        a.get("name") for a in (track.get("artists") or []) if isinstance(a, dict) and a.get("name")
    ]
    if not artists:
        return None, "no_artist"
    album = (track.get("album") or {}).get("name")
    return (
        {
            "position": position,
            "spotify_id": spotify_id,
            "title": title,
            "artists": artists,
            "album": album,
            "duration_ms": track.get("duration_ms"),
            "isrc": (track.get("external_ids") or {}).get("isrc"),
            "explicit": 1 if track.get("explicit") else 0,
            "is_local": 0,
            "added_at": item.get("added_at"),
        },
        "",
    )
