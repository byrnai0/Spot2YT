from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from rich.panel import Panel

from . import __version__
from .commands import (
    apply_review,
    doctor,
    export_playlist,
    find_tracks,
    import_playlist,
    match_playlist,
    run_all,
    setup_guide,
    show_playlists,
    show_status,
    spotify_login,
    sync_playlist,
    write_review,
    yt_login_headers,
    yt_login_oauth,
)
from .config import Settings
from .db import connect
from .errors import AuthError, S2YError
from .ui import console


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="s2y",
        description="Transfer Spotify playlists to YouTube Music, locally and resumably.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical flow:\n"
            "  s2y setup\n"
            "  s2y spotify-login\n"
            "  s2y yt-login --from-clipboard\n"
            "  s2y export \"My playlist\"\n"
            "  s2y match \"My playlist\"\n"
            "  s2y review \"My playlist\"\n"
            "  s2y sync \"My playlist\"\n"
        ),
    )
    parser.add_argument(
        "--home",
        metavar="DIR",
        help="State directory (default: %%USERPROFILE%%\\.spotify2yt)",
    )
    parser.add_argument("--debug", action="store_true", help="Show tracebacks")
    parser.add_argument(
        "--version", action="version", version=f"s2y {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Create .env and print the setup guide")

    sub.add_parser("doctor", help="Check config, logins and connectivity")

    sub.add_parser("spotify-login", help="Open the Spotify PKCE login flow")

    yt = sub.add_parser("yt-login", help="Save YouTube Music browser headers/OAuth")
    yt_group = yt.add_mutually_exclusive_group()
    yt_group.add_argument(
        "--headers-file",
        metavar="PATH",
        help="Text file containing copied browser request headers",
    )
    yt_group.add_argument(
        "--from-clipboard",
        action="store_true",
        help="Read copied request headers from the Windows clipboard",
    )
    yt_group.add_argument(
        "--paste",
        action="store_true",
        help="Read headers from stdin (Ctrl+Z then Enter on Windows)",
    )
    yt_group.add_argument(
        "--oauth",
        action="store_true",
        help="Use a Google Cloud OAuth client instead of browser headers",
    )

    pl = sub.add_parser("playlists", help="List Spotify playlists (or local ones)")
    pl.add_argument("--limit", type=int, default=None, help="Show at most N")
    pl.add_argument("--local", action="store_true", help="List exported playlists")

    exp = sub.add_parser("export", help="Snapshot a Spotify playlist into SQLite")
    exp.add_argument("playlist", help="Spotify URL, URI, ID, or playlist name")

    imp = sub.add_parser(
        "import",
        help="Import a CSV/JSON track list without the Spotify API",
    )
    imp.add_argument("file", help="CSV or JSON export file")
    imp.add_argument("--name", help="Playlist name (default: file name or JSON name)")

    mat = sub.add_parser("match", help="Search YouTube Music and score candidates")
    mat.add_argument("playlist", help="Name or Spotify ID (must be exported)")
    mat.add_argument("--limit", type=int, default=None, help="Only process N tracks")
    mat.add_argument("--force", action="store_true", help="Re-check existing matches")
    mat.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore the local YouTube search cache",
    )
    mat.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Seconds between YouTube requests (default: 1.0)",
    )

    rev = sub.add_parser("review", help="Write a CSV of low-confidence matches")
    rev.add_argument("playlist", help="Name or Spotify ID")
    rev.add_argument("--output", metavar="PATH", help="CSV path")

    app = sub.add_parser("apply-review", help="Apply the filled-in review CSV")
    app.add_argument("playlist", help="Name or Spotify ID")
    app.add_argument("csv_file", metavar="CSV", help="Edited review CSV")

    syn = sub.add_parser("sync", help="Create/update the YouTube Music playlist")
    syn.add_argument("playlist", help="Name or Spotify ID")
    syn.add_argument("--name", help="YouTube playlist title (default: Spotify title)")
    syn.add_argument(
        "--privacy",
        type=str.upper,
        choices=["PRIVATE", "UNLISTED", "PUBLIC"],
        default="PRIVATE",
        help="Playlist visibility (default: PRIVATE)",
    )
    syn.add_argument(
        "--playlist-id",
        metavar="ID",
        help="Add to this existing YouTube Music playlist instead",
    )
    syn.add_argument("--limit", type=int, default=None, help="Only add N tracks")
    syn.add_argument("--dry-run", action="store_true", help="Show what would be added")
    syn.add_argument(
        "--new",
        action="store_true",
        help="Create a new playlist instead of reusing the linked/equal-name one",
    )
    syn.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Seconds between YouTube requests (default: 1.0)",
    )

    st = sub.add_parser("status", help="Show match/add progress for a playlist")
    st.add_argument("playlist", help="Name or Spotify ID")

    fnd = sub.add_parser("find", help="Test a YouTube Music search")
    fnd.add_argument("query", help="Search query")
    fnd.add_argument("--limit", type=int, default=10)
    fnd.add_argument("--songs", action="store_true", help="Songs filter only")
    fnd.add_argument("--delay", type=float, default=None)

    run = sub.add_parser("run", help="export + match + sync in one go")
    run.add_argument("playlist", help="Spotify URL, URI, ID, or playlist name")
    run.add_argument("--limit", type=int, default=None, help="Only match N tracks")
    run.add_argument(
        "--privacy",
        type=str.upper,
        choices=["PRIVATE", "UNLISTED", "PUBLIC"],
        default="PRIVATE",
    )
    run.add_argument("--name", help="YouTube playlist title")
    run.add_argument("--delay", type=float, default=None)

    return parser


def _read_headers(args: argparse.Namespace) -> str:
    if args.headers_file:
        path = Path(args.headers_file).expanduser()
        if not path.exists():
            raise AuthError(f"Headers file not found: {path}")
        return path.read_text(encoding="utf-8", errors="replace")

    if args.from_clipboard:
        if sys.platform != "win32":
            raise AuthError(
                "--from-clipboard currently supports Windows only. "
                "Use --headers-file instead."
            )
        try:
            return subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
        except Exception as exc:
            raise AuthError(f"Could not read the clipboard: {exc}") from exc

    console.print(
        "Paste the copied request headers below, then press [bold]Ctrl+Z[/] "
        "and [bold]Enter[/] (Windows) or [bold]Ctrl+D[/] (macOS/Linux):"
    )
    return sys.stdin.read()


def dispatch(settings: Settings, conn, args: argparse.Namespace) -> None:
    if args.command == "setup":
        setup_guide(settings)
    elif args.command == "doctor":
        doctor(settings, conn)
    elif args.command == "spotify-login":
        spotify_login(settings)
    elif args.command == "yt-login":
        if args.oauth:
            yt_login_oauth(settings)
        else:
            if not (args.headers_file or args.from_clipboard or args.paste):
                args.paste = True
            yt_login_headers(settings, _read_headers(args))
    elif args.command == "playlists":
        show_playlists(settings, conn, args.limit, args.local)
    elif args.command == "export":
        export_playlist(settings, conn, args.playlist)
    elif args.command == "import":
        import_playlist(settings, conn, args.file, args.name)
    elif args.command == "match":
        match_playlist(
            settings,
            conn,
            args.playlist,
            limit=args.limit,
            force=args.force,
            refresh=args.refresh,
            delay=args.delay,
        )
    elif args.command == "review":
        write_review(settings, conn, args.playlist, args.output)
    elif args.command == "apply-review":
        apply_review(settings, conn, args.playlist, args.csv_file)
    elif args.command == "sync":
        sync_playlist(
            settings,
            conn,
            args.playlist,
            name=args.name,
            privacy=args.privacy,
            playlist_id=args.playlist_id,
            limit=args.limit,
            dry_run=args.dry_run,
            new=args.new,
            delay=args.delay,
        )
    elif args.command == "status":
        show_status(settings, conn, args.playlist)
    elif args.command == "find":
        find_tracks(
            settings,
            args.query,
            limit=args.limit,
            songs_only=args.songs,
            delay=args.delay,
        )
    elif args.command == "run":
        run_all(
            settings,
            conn,
            args.playlist,
            match_limit=args.limit,
            delay=args.delay,
            privacy=args.privacy,
            name=args.name,
        )
    else:
        raise S2YError(f"Unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:
                pass

    parser = build_parser()
    args = parser.parse_args(argv)

    conn = None
    try:
        settings = Settings.load(args.home)
        conn = connect(settings.db_path)
        dispatch(settings, conn, args)
        return 0
    except S2YError as exc:
        console.print(Panel(str(exc), title="Error", border_style="red"))
        return 1
    except KeyboardInterrupt:
        console.print(
            "\n[yellow]Interrupted. Progress is saved; rerun the same command "
            "to continue.[/]"
        )
        return 130
    except Exception as exc:
        if args.debug:
            raise
        console.print(
            Panel(
                f"{type(exc).__name__}: {exc}\n\n"
                "Rerun with [bold]--debug[/] (before the command) for a traceback.",
                title="Unexpected error",
                border_style="red",
            )
        )
        return 1
    finally:
        if conn is not None:
            conn.close()
