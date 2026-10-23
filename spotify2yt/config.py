from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import S2YError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"


def _env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise S2YError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise S2YError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    home: Path
    db_path: Path
    exports_dir: Path
    spotify_client_id: str | None
    spotify_client_secret: str | None
    spotify_redirect_uri: str
    spotify_cache_path: Path
    yt_auth_path: Path
    delay: float
    auto_threshold: float
    review_threshold: float
    search_limit: int
    batch_size: int
    cache_ttl_days: int
    google_client_id: str | None
    google_client_secret: str | None

    @classmethod
    def load(cls, home: str | Path | None = None) -> "Settings":
        # Project .env wins for developer convenience, but only fills unset vars.
        load_dotenv(PROJECT_ROOT / ".env", override=False)

        home_dir = Path(
            home or _env("S2Y_HOME") or (Path.home() / ".spotify2yt")
        ).expanduser()
        home_dir = home_dir.resolve()
        home_dir.mkdir(parents=True, exist_ok=True)
        # A per-home .env is also supported.
        load_dotenv(home_dir / ".env", override=False)

        auth_dir = home_dir / "auth"
        auth_dir.mkdir(parents=True, exist_ok=True)
        exports_dir = home_dir / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)

        client_id = _env("SPOTIFY_CLIENT_ID", "SPOTIPY_CLIENT_ID")
        client_secret = _env("SPOTIFY_CLIENT_SECRET", "SPOTIPY_CLIENT_SECRET")
        redirect_uri = _env(
            "SPOTIFY_REDIRECT_URI",
            "SPOTIPY_REDIRECT_URI",
            default=DEFAULT_REDIRECT_URI,
        )

        yt_auth = Path(
            _env("S2Y_YT_AUTH", default=str(auth_dir / "browser.json"))
        ).expanduser()
        db_path = Path(
            _env("S2Y_DB", default=str(home_dir / "state.db"))
        ).expanduser()

        auto_threshold = _env_float("S2Y_AUTO_THRESHOLD", 0.80)
        review_threshold = _env_float("S2Y_REVIEW_THRESHOLD", 0.50)
        if not 0 <= review_threshold <= auto_threshold <= 1:
            raise S2YError(
                "Thresholds must satisfy 0 <= S2Y_REVIEW_THRESHOLD <= "
                "S2Y_AUTO_THRESHOLD <= 1"
            )

        return cls(
            home=home_dir,
            db_path=db_path,
            exports_dir=exports_dir,
            spotify_client_id=client_id,
            spotify_client_secret=client_secret,
            spotify_redirect_uri=redirect_uri or DEFAULT_REDIRECT_URI,
            spotify_cache_path=auth_dir / "spotify_token.json",
            yt_auth_path=yt_auth,
            delay=max(0.0, _env_float("S2Y_DELAY", 1.0)),
            auto_threshold=auto_threshold,
            review_threshold=review_threshold,
            search_limit=max(1, _env_int("S2Y_SEARCH_LIMIT", 8)),
            batch_size=max(1, _env_int("S2Y_BATCH_SIZE", 20)),
            cache_ttl_days=max(0, _env_int("S2Y_CACHE_TTL_DAYS", 30)),
            google_client_id=_env("S2Y_GOOGLE_CLIENT_ID"),
            google_client_secret=_env("S2Y_GOOGLE_CLIENT_SECRET"),
        )
