from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import ytmusicapi
from rich.panel import Panel
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from .config import Settings
from .errors import AuthError, S2YError
from .importer import read_import
from .matching import build_query, classify, rank_candidates, TrackInfo
from .spotify_api import (
    current_user_name,
    item_to_track,
    iter_playlist_items,
    list_user_playlists,
    make_spotify,
    probe_spotify,
    resolve_playlist,
)
from .store import (
    cache_get,
    cache_key,
    cache_put,
    candidates_for_track,
    clear_yt_target,
    list_local_playlists,
    mark_added,
    mark_duplicate,
    mark_failed,
    mark_unmatched,
    matching_rows,
    replace_tracks,
    resolve_local_playlist,
    review_rows,
    save_error,
    save_match,
    set_manual,
    set_yt_target,
    status_counts,
    sync_rows,
    track_info,
    upsert_playlist,
)
from .ui import console, dim, err, info, ok, playlist_table, warn
from .util import (
    chunks,
    extract_youtube_id,
    fmt_duration,
    json_load,
    slugify,
    utcnow,
)
from .youtube_api import (
    YTClient,
    auth_kind,
    is_auth_error,
    is_not_found,
    make_ytmusic,
)

STATUS_MEANING = {
    "pending": "not searched yet",
    "auto": "auto-matched, ready to add",
    "review": "low confidence, needs your choice",
    "manual": "you chose the video, ready to add",
    "unmatched": "no acceptable result",
    "added": "already in the YouTube playlist",
    "failed": "YouTube rejected it; rerun sync",
    "duplicate": "same video appears earlier in the playlist",
    "error": "search failed; rerun match",
}

# Prefer `s2y` when the venv is on PATH; otherwise point at the wrapper.
LAUNCHER = "s2y" if shutil.which("s2y") else (r".\s2y.cmd" if os.name == "nt" else "s2y")


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _artists(value: Any) -> list[str]:
    if isinstance(value, str):
        return list(json_load(value, []))
    return list(value or [])


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )


def _next_step(playlist_name: str, action: str) -> None:
    dim(f'next: {LAUNCHER} {action} "{playlist_name}"')


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_playlist(settings: Settings, conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    sp = make_spotify(settings)
    info("Reading Spotify playlist metadata ...")
    meta = resolve_playlist(sp, ref)
    upsert_playlist(conn, meta)
    total = meta.get("total") or 0

    tracks: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    with _progress() as progress:
        task = progress.add_task(f"Exporting {escape(meta['name'])}", total=total or None)
        for position, item in iter_playlist_items(sp, meta["id"]):
            track, reason = item_to_track(item, position)
            if reason:
                skipped[reason] += 1
            else:
                tracks.append(track)
            if total:
                progress.update(task, completed=min(len(tracks) + sum(skipped.values()), total))
    if total == 0:
        # Progress with total=None still needs a finish line.
        console.print()

    inserted, updated, removed = replace_tracks(conn, meta["id"], tracks)

    snapshot = {
        "exported_at": utcnow(),
        "playlist": meta,
        "skipped": dict(skipped),
        "tracks": tracks,
    }
    snapshot_path = (
        settings.exports_dir / f"{slugify(meta['name'])}-{_timestamp()}.json"
    )
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    table = Table(title=f"Exported: {escape(meta['name'])}", show_header=False)
    table.add_row("Spotify ID", escape(meta["id"]))
    table.add_row("Tracks stored", str(len(tracks)))
    table.add_row("New / updated / removed", f"{inserted} / {updated} / {removed}")
    if meta.get("owner"):
        table.add_row("Owner", escape(meta["owner"]))
    if skipped:
        table.add_row(
            "Skipped",
            ", ".join(f"{reason}: {count}" for reason, count in skipped.items()),
        )
    console.print(table)
    dim(f"snapshot: {snapshot_path}")
    ok("Spotify export complete.")
    _next_step(meta["name"], "match")
    return meta


def import_playlist(
    settings: Settings,
    conn: sqlite3.Connection,
    file: str,
    name: str | None = None,
) -> None:
    path = Path(file).expanduser()
    playlist_name, tracks = read_import(path, name)
    digest = hashlib.sha1(
        f"{playlist_name}|{path.resolve()}".encode("utf-8")
    ).hexdigest()[:10]
    playlist_id = f"import:{slugify(playlist_name, 40)}-{digest}"
    meta = {
        "id": playlist_id,
        "name": playlist_name,
        "owner": "local import",
        "description": f"Imported from {path.name}",
        "snapshot_id": None,
        "total": len(tracks),
    }
    upsert_playlist(conn, meta)
    inserted, updated, removed = replace_tracks(conn, playlist_id, tracks)

    snapshot_path = (
        settings.exports_dir / f"import-{slugify(playlist_name)}-{_timestamp()}.json"
    )
    snapshot_path.write_text(
        json.dumps(
            {"imported_at": utcnow(), "playlist": meta, "tracks": tracks},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    table = Table(title=f"Imported: {escape(playlist_name)}", show_header=False)
    table.add_row("Local ID", escape(playlist_id))
    table.add_row("Tracks stored", str(len(tracks)))
    table.add_row("New / updated / removed", f"{inserted} / {updated} / {removed}")
    table.add_row("Source file", escape(str(path)))
    console.print(table)
    dim(f"snapshot: {snapshot_path}")
    ok("Imported without using the Spotify API.")
    info(
        "Matching and syncing work the same. Use this playlist ID if the name "
        "is ambiguous."
    )
    _next_step(playlist_id, "match")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def match_playlist(
    settings: Settings,
    conn: sqlite3.Connection,
    ref: str,
    limit: int | None = None,
    force: bool = False,
    refresh: bool = False,
    delay: float | None = None,
) -> None:
    playlist = resolve_local_playlist(conn, ref)
    playlist_id = playlist["spotify_id"]
    rows = matching_rows(conn, playlist_id, force=force)
    if limit:
        rows = rows[:limit]
    if not rows:
        ok("Nothing left to match. Use --force to re-check existing matches.")
        return

    yt = YTClient(
        make_ytmusic(settings),
        delay=settings.delay if delay is None else max(0.0, delay),
    )
    counts: Counter[str] = Counter()
    problems: list[str] = []

    with _progress() as progress:
        task = progress.add_task(f"Matching {escape(playlist['name'])}", total=len(rows))
        for row in rows:
            try:
                track = track_info(row)
                query = build_query(track)
                key = cache_key(query, "songs")
                results = None if refresh else cache_get(
                    conn, key, settings.cache_ttl_days
                )
                if results is None:
                    results = yt.search(
                        query, filter_name="songs", limit=settings.search_limit
                    )
                    if not results:
                        # Music videos / user uploads sometimes exist when no
                        # "song" entry does.
                        results = yt.search(
                            query, filter_name=None, limit=settings.search_limit
                        )
                    cache_put(conn, key, query, results if isinstance(results, list) else [])

                candidates = rank_candidates(track, results or [], limit=5)
                best = candidates[0] if candidates else None
                if best is None:
                    status = "unmatched"
                    save_match(
                        conn, row["id"], status, [], None, note="no search results"
                    )
                else:
                    status = classify(
                        best.score,
                        best.breakdown,
                        settings.auto_threshold,
                        settings.review_threshold,
                    )
                    note = None
                    if best.breakdown.get("hard_mismatch"):
                        note = "version mismatch (live/remix/cover/etc.)"
                    save_match(conn, row["id"], status, candidates, best, note=note)
                counts[status] += 1
            except AuthError:
                raise
            except Exception as exc:
                if is_auth_error(exc):
                    raise AuthError(
                        "YouTube Music rejected the request. Your browser headers "
                        "probably expired; run `s2y yt-login` again."
                    ) from exc
                message = f"position {row['position']}: {type(exc).__name__}: {exc}"
                problems.append(message)
                save_error(conn, row["id"], message)
                counts["error"] += 1
            finally:
                progress.advance(task)

    table = Table(title=f"Matched: {escape(playlist['name'])}", show_header=True)
    table.add_column("Status")
    table.add_column("Tracks", justify="right")
    for status in ("auto", "review", "unmatched", "error"):
        if counts.get(status):
            table.add_row(status, str(counts[status]))
    console.print(table)

    if problems:
        warn(f"{len(problems)} search error(s). First few:")
        for message in problems[:5]:
            console.print(f"  [dim]{message}[/]")

    review_count = status_counts(conn, playlist_id).get("review", 0)
    unmatched_count = status_counts(conn, playlist_id).get("unmatched", 0)
    if review_count or unmatched_count:
        info(
            f"{review_count} need review and {unmatched_count} are unmatched. "
            "Fix them in a spreadsheet:"
        )
        _next_step(playlist["name"], "review")
    else:
        ok("Everything matched automatically.")
        _next_step(playlist["name"], "sync")


# ---------------------------------------------------------------------------
# Review CSV
# ---------------------------------------------------------------------------


def _format_candidate(index: int, candidate: dict[str, Any]) -> str:
    artists = ", ".join(candidate.get("artists") or []) or "?"
    duration = fmt_duration(candidate.get("duration"))
    return (
        f"{index}) {candidate.get('title')} - {artists} "
        f"({duration}) [{candidate.get('video_id')}] "
        f"score={float(candidate.get('score') or 0):.2f}"
    )


def write_review(
    settings: Settings,
    conn: sqlite3.Connection,
    ref: str,
    output: str | None = None,
) -> Path:
    playlist = resolve_local_playlist(conn, ref)
    rows = review_rows(conn, playlist["spotify_id"])
    if not rows:
        ok("Nothing to review: every track is auto-matched, manual, or already added.")
        return Path()

    path = (
        Path(output).expanduser()
        if output
        else settings.exports_dir
        / f"review-{slugify(playlist['name'])}-{_timestamp()}.csv"
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    columns = [
        "position",
        "spotify_title",
        "spotify_artists",
        "spotify_album",
        "spotify_duration",
        "status",
        "score",
        "suggestion",
        "candidate_ids",
        "candidates",
        "choice",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            candidates = json_load(row["candidates"], [])
            if not isinstance(candidates, list):
                candidates = []
            candidate_lines = [
                _format_candidate(index, candidate)
                for index, candidate in enumerate(candidates, 1)
            ]
            writer.writerow(
                {
                    "position": row["position"],
                    "spotify_title": row["title"],
                    "spotify_artists": ", ".join(_artists(row["artists"])),
                    "spotify_album": row["album"] or "",
                    "spotify_duration": fmt_duration(
                        (row["duration_ms"] or 0) / 1000 if row["duration_ms"] else None
                    ),
                    "status": row["status"],
                    "score": f"{row['score']:.3f}" if row["score"] is not None else "",
                    "suggestion": candidate_lines[0] if candidate_lines else "",
                    "candidate_ids": "|".join(
                        str(candidate.get("video_id")) for candidate in candidates
                    ),
                    "candidates": " || ".join(candidate_lines),
                    "choice": "",
                }
            )

    counts = Counter(row["status"] for row in rows)
    table = Table(title="Review file written", show_header=False)
    table.add_row("Rows", str(len(rows)))
    table.add_row(
        "By status", ", ".join(f"{status}: {count}" for status, count in counts.items())
    )
    console.print(table)
    console.print(f"[bold]File:[/] {escape(str(path))}")
    console.print(
        Panel(
            "[bold]How to review[/]\n"
            '1. Open the CSV (Excel is fine).\n'
            '2. For each row, put a number (1-5) in the [bold]choice[/] column to '
            "pick a candidate.\n"
            "   You can also paste a YouTube URL/video ID, or type [bold]skip[/].\n"
            "3. Leave [bold]choice[/] blank to leave the row alone.\n"
            "4. Save, then run:\n"
            f'   [cyan]{LAUNCHER} apply-review "{escape(playlist["name"])}" '
            f'"{escape(str(path))}"[/]',
            border_style="cyan",
        )
    )
    return path


def apply_review(
    settings: Settings, conn: sqlite3.Connection, ref: str, csv_path: str
) -> None:
    import re

    playlist = resolve_local_playlist(conn, ref)
    path = Path(csv_path).expanduser()
    if not path.exists():
        raise S2YError(f"Review file not found: {path}")

    applied = skipped = marked_skip = 0
    errors: list[str] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "choice" not in reader.fieldnames:
            raise S2YError("That CSV has no 'choice' column. Is it the review file?")
        for line_number, row in enumerate(reader, 2):
            choice = (row.get("choice") or "").strip()
            if not choice:
                skipped += 1
                continue
            try:
                position = int(float(row.get("position") or ""))
            except ValueError:
                errors.append(f"line {line_number}: bad position")
                continue

            track = conn.execute(
                "SELECT id FROM tracks WHERE playlist_id = ? AND position = ?",
                (playlist["spotify_id"], position),
            ).fetchone()
            if not track:
                errors.append(f"line {line_number}: position {position} not found")
                continue
            track_id = track["id"]

            if choice.casefold() in ("skip", "x", "none", "no", "-"):
                mark_unmatched(conn, track_id, "skipped during review")
                marked_skip += 1
                continue

            candidates = candidates_for_track(conn, playlist["spotify_id"], position)
            selected: dict[str, Any] | None = None
            video_id: str | None = None

            if choice.isdigit() or re.fullmatch(r"\d+\.0+", choice):
                index = int(float(choice))
                if not 1 <= index <= len(candidates):
                    errors.append(
                        f"line {line_number}: choice {index} but only "
                        f"{len(candidates)} candidate(s)"
                    )
                    continue
                selected = candidates[index - 1]
                video_id = str(selected.get("video_id") or "")
            else:
                video_id = extract_youtube_id(choice)
                if not video_id:
                    errors.append(f"line {line_number}: not a video ID/URL: {choice!r}")
                    continue
                selected = next(
                    (
                        candidate
                        for candidate in candidates
                        if str(candidate.get("video_id")) == video_id
                    ),
                    None,
                )

            if not video_id:
                errors.append(f"line {line_number}: candidate has no video id")
                continue
            set_manual(conn, track_id, video_id, selected)
            applied += 1

    table = Table(title="Review applied", show_header=False)
    table.add_row("Manual choices", str(applied))
    table.add_row("Marked skip/unmatched", str(marked_skip))
    table.add_row("Blank rows left alone", str(skipped))
    if errors:
        table.add_row("Errors", str(len(errors)))
    console.print(table)
    for message in errors[:15]:
        err(message)
    if errors:
        warn("Fix those rows and run apply-review again (other rows are already saved).")
    if applied or marked_skip:
        _next_step(playlist["name"], "sync")


# ---------------------------------------------------------------------------
# Sync to YouTube Music
# ---------------------------------------------------------------------------


def _ensure_target(
    settings: Settings,
    conn: sqlite3.Connection,
    yt: YTClient,
    playlist: dict[str, Any],
    desired_name: str,
    privacy: str,
    playlist_id: str | None,
    new: bool,
    dry_run: bool,
) -> tuple[str | None, str, bool]:
    if playlist_id:
        summary = yt.get_playlist_summary(playlist_id)
        title = summary.get("title") or desired_name
        set_yt_target(conn, playlist["spotify_id"], playlist_id, title)
        return playlist_id, title, False

    stored_id = playlist.get("yt_playlist_id")
    if stored_id and not new:
        try:
            summary = yt.get_playlist_summary(stored_id)
            title = summary.get("title") or playlist.get("yt_playlist_name") or desired_name
            return stored_id, title, False
        except Exception as exc:
            if is_auth_error(exc):
                raise AuthError(
                    "YouTube Music auth expired. Run `s2y yt-login` again."
                ) from exc
            if is_not_found(exc):
                warn("The previously linked YouTube playlist no longer exists.")
                clear_yt_target(conn, playlist["spotify_id"])
            else:
                raise

    if not new:
        found = yt.find_library_playlist(desired_name)
        if found:
            found_id = found.get("playlistId") or found.get("id")
            if found_id:
                title = found.get("title") or desired_name
                set_yt_target(conn, playlist["spotify_id"], str(found_id), title)
                info(f"Reusing existing YouTube Music playlist: {title}")
                return str(found_id), title, False

    if dry_run:
        return None, desired_name, False

    description = (
        f"Imported from Spotify: https://open.spotify.com/playlist/{playlist['spotify_id']}"
    )
    if playlist["spotify_id"].startswith("import:"):
        description = (
            "Imported from a local track list: "
            f"{playlist.get('description') or 'CSV/JSON file'}"
        )
    if playlist.get("owner"):
        description += f"\nSpotify owner: {playlist['owner']}"
    new_id = yt.create_playlist(desired_name, description, privacy)
    set_yt_target(conn, playlist["spotify_id"], new_id, desired_name)
    ok(f"Created YouTube Music playlist: {desired_name}")
    return new_id, desired_name, True


def sync_playlist(
    settings: Settings,
    conn: sqlite3.Connection,
    ref: str,
    name: str | None = None,
    privacy: str = "PRIVATE",
    playlist_id: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    new: bool = False,
    delay: float | None = None,
) -> None:
    playlist = resolve_local_playlist(conn, ref)
    playlist_id_db = playlist["spotify_id"]
    yt = YTClient(
        make_ytmusic(settings),
        delay=settings.delay if delay is None else max(0.0, delay),
    )

    desired_name = name or playlist["name"]
    target_id, target_name, created = _ensure_target(
        settings, conn, yt, playlist, desired_name, privacy, playlist_id, new, dry_run
    )

    if target_id:
        existing = yt.playlist_video_ids(target_id)
        info(f"Target: {target_name} ({len(existing)} videos already there)")
    else:
        existing = set()
        info(f"Dry run: would create YouTube playlist {desired_name!r}")

    rows = sync_rows(conn, playlist_id_db)
    reconciled = 0
    for row in rows:
        if row["video_id"] in existing and row["status"] != "added":
            mark_added(conn, row["id"])
            reconciled += 1
    rows = [row for row in rows if row["video_id"] not in existing]

    seen: dict[str, int] = {}
    unique: list[sqlite3.Row] = []
    duplicates = 0
    for row in rows:
        video_id = row["video_id"]
        if video_id in seen:
            if row["status"] != "added":
                mark_duplicate(
                    conn,
                    row["id"],
                    f"same video as position {seen[video_id]}",
                )
                duplicates += 1
            continue
        seen[video_id] = row["position"]
        unique.append(row)
    conn.commit()

    if limit:
        unique = unique[:limit]

    if dry_run:
        table = Table(title="Dry run: would add")
        table.add_column("Pos", justify="right")
        table.add_column("Spotify track")
        table.add_column("YouTube video")
        table.add_column("Score", justify="right")
        for row in unique[:20]:
            table.add_row(
                str(row["position"]),
                escape(f"{row['title']} - {', '.join(_artists(row['artists']))}"),
                f"{escape(row['yt_title'] or '')}\n[dim]{escape(row['video_id'] or '')}[/]",
                f"{row['score']:.2f}" if row["score"] is not None else "",
            )
        console.print(table)
        if len(unique) > 20:
            dim(f"... and {len(unique) - 20} more")
        info(
            f"{len(unique)} to add, {len(existing)} already present, "
            f"{duplicates} duplicate(s), {reconciled} reconciled."
        )
        return

    if not unique:
        ok("YouTube playlist is already up to date.")
        pending = status_counts(conn, playlist_id_db)
        if pending.get("review") or pending.get("unmatched"):
            info(
                f"{pending.get('review', 0)} review + "
                f"{pending.get('unmatched', 0)} unmatched still need attention. "
                "Run `s2y review`."
            )
        return

    added = failed = 0
    with _progress() as progress:
        task = progress.add_task(
            f"Adding to {escape(target_name)}", total=len(unique)
        )
        for batch in chunks(unique, settings.batch_size):
            ids = [row["video_id"] for row in batch]
            try:
                yt.add_items(str(target_id), ids)
                for row in batch:
                    mark_added(conn, row["id"])
                added += len(batch)
            except Exception as exc:
                if is_auth_error(exc):
                    raise AuthError(
                        "YouTube Music auth expired while adding. "
                        "Run `s2y yt-login` again, then rerun sync."
                    ) from exc
                warn(f"Batch failed ({exc}); retrying tracks one by one ...")
                for row in batch:
                    try:
                        yt.add_items(str(target_id), [row["video_id"]])
                        mark_added(conn, row["id"])
                        added += 1
                    except Exception as inner:
                        if is_auth_error(inner):
                            raise AuthError(
                                "YouTube Music auth expired during sync. "
                                "Run `s2y yt-login` again."
                            ) from inner
                        mark_failed(conn, row["id"], str(inner))
                        failed += 1
            conn.commit()
            progress.advance(task, len(batch))

    table = Table(title=f"Sync complete: {escape(target_name)}", show_header=False)
    table.add_row("Added this run", str(added))
    table.add_row("Already present", str(len(existing)))
    table.add_row("Reconciled (state was stale)", str(reconciled))
    table.add_row("Duplicates skipped", str(duplicates))
    if failed:
        table.add_row("Failed", str(failed))
    console.print(table)
    console.print(f"[bold]Open:[/] https://music.youtube.com/playlist?list={target_id}")
    if failed:
        warn("Some tracks failed. Rerun `s2y sync` to retry them.")
    else:
        ok("Done. Open the link above and check a few tracks.")

    pending = status_counts(conn, playlist_id_db)
    if pending.get("review") or pending.get("unmatched"):
        info(
            f"{pending.get('review', 0)} review + {pending.get('unmatched', 0)} "
            "unmatched tracks are not in the playlist yet. Run `s2y review`."
        )


# ---------------------------------------------------------------------------
# Status / listing / auth
# ---------------------------------------------------------------------------


def show_status(settings: Settings, conn: sqlite3.Connection, ref: str) -> None:
    playlist = resolve_local_playlist(conn, ref)
    counts = status_counts(conn, playlist["spotify_id"])
    total = sum(counts.values())
    table = Table(title=f"{escape(playlist['name'])} - {total} tracks")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    table.add_column("Meaning")
    for status in (
        "added",
        "auto",
        "manual",
        "review",
        "unmatched",
        "pending",
        "failed",
        "duplicate",
        "error",
    ):
        if counts.get(status):
            table.add_row(status, str(counts[status]), STATUS_MEANING.get(status, ""))
    console.print(table)

    if playlist.get("yt_playlist_id"):
        console.print(
            "[bold]YouTube playlist:[/] "
            f"https://music.youtube.com/playlist?list={playlist['yt_playlist_id']}"
        )
    else:
        dim("No YouTube playlist linked yet.")

    if counts.get("pending") or counts.get("error"):
        _next_step(playlist["name"], "match")
    if counts.get("review") or counts.get("unmatched"):
        _next_step(playlist["name"], "review")
    if counts.get("auto") or counts.get("manual") or counts.get("failed"):
        _next_step(playlist["name"], "sync")

    rows = review_rows(conn, playlist["spotify_id"])[:8]
    if rows:
        console.print()
        sample = Table(title="Needs attention (first 8)")
        sample.add_column("Pos", justify="right")
        sample.add_column("Spotify")
        sample.add_column("Status")
        sample.add_column("Best candidate")
        for row in rows:
            candidates = json_load(row["candidates"], [])
            best = candidates[0] if isinstance(candidates, list) and candidates else None
            sample.add_row(
                str(row["position"]),
                escape(f"{row['title']} - {', '.join(_artists(row['artists']))}"),
                row["status"],
                (
                    escape(f"{best.get('title')} [{best.get('video_id')}] ")
                    + f"{float(best.get('score') or 0):.2f}"
                    if best
                    else "-"
                ),
            )
        console.print(sample)


def show_playlists(
    settings: Settings, conn: sqlite3.Connection, limit: int | None, local: bool
) -> None:
    if local:
        rows = list_local_playlists(conn)
        table = Table(title="Local playlists")
        table.add_column("Name")
        table.add_column("Tracks", justify="right")
        table.add_column("Spotify ID", style="dim")
        table.add_column("YouTube playlist", style="dim")
        for row in rows[: limit or None]:
            table.add_row(
                escape(row["name"]),
                str(row.get("track_count") or 0),
                escape(row["spotify_id"]),
                escape(row.get("yt_playlist_id") or ""),
            )
        console.print(table)
        return

    sp = make_spotify(settings)
    rows = list_user_playlists(sp)
    if limit:
        rows = rows[:limit]
    console.print(playlist_table(rows))
    info(f'Export one with: {LAUNCHER} export "<name or id>"')


def find_tracks(
    settings: Settings,
    query: str,
    limit: int = 10,
    songs_only: bool = False,
    delay: float | None = None,
) -> None:
    yt = YTClient(
        make_ytmusic(settings, required=False),
        delay=settings.delay if delay is None else max(0.0, delay),
    )
    results = yt.search(
        query, filter_name="songs" if songs_only else None, limit=limit
    )
    table = Table(title=f"YouTube Music search: {escape(query)}")
    table.add_column("#", justify="right")
    table.add_column("Title")
    table.add_column("Artists")
    table.add_column("Album")
    table.add_column("Time", justify="right")
    table.add_column("Video ID", style="dim")
    for index, raw in enumerate(results, 1):
        artists = []
        for artist in raw.get("artists") or []:
            if isinstance(artist, dict):
                artists.append(artist.get("name") or "")
            else:
                artists.append(str(artist))
        album = raw.get("album")
        if isinstance(album, dict):
            album = album.get("name")
        table.add_row(
            str(index),
            escape(raw.get("title") or ""),
            escape(", ".join(filter(None, artists))),
            escape(str(album or "")),
            fmt_duration(raw.get("duration_seconds")),
            escape(raw.get("videoId") or ""),
        )
    console.print(table)


def spotify_login(settings: Settings) -> None:
    sp = make_spotify(settings)
    ok(f"Logged in to Spotify as {current_user_name(sp)}.")


def yt_login_headers(settings: Settings, headers_raw: str) -> None:
    if not headers_raw.strip():
        raise AuthError("No headers were provided.")
    path = settings.yt_auth_path
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{_timestamp()}")
        shutil.copy2(path, backup)
    try:
        message = ytmusicapi.setup(filepath=str(path), headers_raw=headers_raw)
    except Exception as exc:
        raise AuthError(
            "YouTube Music rejected those headers. Copy the request headers "
            "again while logged in at https://music.youtube.com.\n\n"
            f"{exc}"
        ) from exc
    if message:
        console.print(message)
    if backup:
        dim(f"Old auth backed up to {backup}")
    _verify_yt_auth(settings)
    ok(f"YouTube Music auth saved to {path}")


def yt_login_oauth(settings: Settings) -> None:
    if not settings.google_client_id or not settings.google_client_secret:
        raise AuthError(
            "OAuth login needs S2Y_GOOGLE_CLIENT_ID and S2Y_GOOGLE_CLIENT_SECRET "
            "from a Google Cloud OAuth client of type 'TVs and Limited Input "
            "devices'. See README. For most people, browser headers are simpler: "
            f"`{LAUNCHER} yt-login`."
        )
    path = settings.yt_auth_path
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{_timestamp()}")
        shutil.copy2(path, backup)
    try:
        ytmusicapi.setup_oauth(
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            filepath=str(path),
            open_browser=True,
        )
    except Exception as exc:
        raise AuthError(f"OAuth login failed: {exc}") from exc
    if backup:
        dim(f"Old auth backed up to {backup}")
    _verify_yt_auth(settings)
    ok(f"YouTube Music OAuth token saved to {path}")


def _verify_yt_auth(settings: Settings) -> None:
    try:
        client = YTClient(make_ytmusic(settings), delay=0.0, retries=1)
        info_data = client.get_account_info()
    except Exception as exc:
        raise AuthError(
            "The auth file was written, but YouTube Music rejected it. "
            "Try copying the headers again (make sure you are logged in).\n\n"
            f"{exc}"
        ) from exc
    name = (
        info_data.get("accountName")
        or info_data.get("displayName")
        or info_data.get("channelHandle")
        or "(account name unavailable)"
    )
    ok(f"YouTube Music connected as {name}.")


def doctor(settings: Settings, conn: sqlite3.Connection) -> None:
    from importlib.metadata import PackageNotFoundError, version

    table = Table(title="s2y doctor")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Details", style="dim", overflow="fold")

    table.add_row("Config home", "OK", escape(str(settings.home)))
    table.add_row("Database", "OK", escape(str(settings.db_path)))
    table.add_row(
        "Spotify Client ID",
        "OK" if settings.spotify_client_id else "MISSING",
        "set" if settings.spotify_client_id else "edit .env and set SPOTIFY_CLIENT_ID",
    )
    table.add_row(
        "Spotify token cache",
        "OK" if settings.spotify_cache_path.exists() else "NOT LOGGED IN",
        escape(str(settings.spotify_cache_path)),
    )
    table.add_row(
        "YouTube auth file",
        "OK" if settings.yt_auth_path.exists() else "NOT LOGGED IN",
        f"{escape(str(settings.yt_auth_path))} ({auth_kind(settings.yt_auth_path)})"
        if settings.yt_auth_path.exists()
        else f"run {LAUNCHER} yt-login",
    )
    for package in ("spotipy", "ytmusicapi", "rapidfuzz", "rich"):
        try:
            table.add_row(package, "OK", version(package))
        except PackageNotFoundError:
            table.add_row(package, "MISSING", "")
    console.print(table)

    if settings.spotify_cache_path.exists():
        try:
            user = probe_spotify(settings)
            if user:
                name = user.get("display_name") or user.get("id") or "unknown"
                ok(f"Spotify API works: {name}")
            else:
                warn("Spotify token cache exists but could not be read.")
        except Exception as exc:
            err(f"Spotify check failed: {exc}")
    else:
        warn(f"Spotify is not logged in yet: {LAUNCHER} spotify-login")

    if settings.yt_auth_path.exists():
        try:
            client = YTClient(make_ytmusic(settings), delay=0.0, retries=1)
            account = client.get_account_info()
            name = account.get("accountName") or account.get("channelHandle") or "unknown"
            ok(f"YouTube Music API works: {name}")
        except Exception as exc:
            err(f"YouTube Music check failed: {exc}")
    else:
        warn(f"YouTube Music is not logged in yet: {LAUNCHER} yt-login")

    local = list_local_playlists(conn)
    if local:
        ok(f"{len(local)} playlist(s) in the local database.")
    else:
        info(f"Local database is empty. Run `{LAUNCHER} playlists` to start.")


def setup_guide(settings: Settings) -> None:
    from .config import PROJECT_ROOT

    env_path = PROJECT_ROOT / ".env"
    example = PROJECT_ROOT / ".env.example"
    if not env_path.exists() and example.exists():
        shutil.copyfile(example, env_path)
        ok(f"Created {env_path}")
    else:
        dim(f"Using existing {env_path}")

    console.print(
        Panel(
            "[bold]1. Spotify (free, official API)[/]\n"
            "   Create an app: https://developer.spotify.com/dashboard\n"
            f"   Add Redirect URI: [cyan]{settings.spotify_redirect_uri}[/]\n"
            f"   Put the Client ID in: [cyan]{env_path}[/]\n"
            f"   Then run: [cyan]{LAUNCHER} spotify-login[/]\n\n"
            "[bold]2. YouTube Music (reverse-engineered private API)[/]\n"
            "   Open https://music.youtube.com and log in.\n"
            "   Press F12 -> Network -> click any [bold]browse[/] POST request.\n"
            "   Right-click it -> Copy -> Copy request headers.\n"
            "   Save them to [cyan]headers.txt[/] (or copy to clipboard), then run:\n"
            f"   [cyan]{LAUNCHER} yt-login --headers-file headers.txt[/]\n"
            f"   or [cyan]{LAUNCHER} yt-login --from-clipboard[/]\n\n"
            "[bold]3. Check[/]\n"
            f"   [cyan]{LAUNCHER} doctor[/]\n\n"
            "[bold]4. Transfer[/]\n"
            f"   [cyan]{LAUNCHER} playlists[/]\n"
            f'   [cyan]{LAUNCHER} export "Playlist name"[/]\n'
            f'   [cyan]{LAUNCHER} match "Playlist name"[/]\n'
            f'   [cyan]{LAUNCHER} review "Playlist name"[/]      (fix low-confidence rows)\n'
            f'   [cyan]{LAUNCHER} sync "Playlist name"[/]\n\n'
            f"State lives in [cyan]{settings.home}[/]. "
            "Auth files are passwords: never commit or share them.",
            title="spotify2yt setup",
            border_style="cyan",
        )
    )


def run_all(
    settings: Settings,
    conn: sqlite3.Connection,
    ref: str,
    match_limit: int | None = None,
    delay: float | None = None,
    privacy: str = "PRIVATE",
    name: str | None = None,
) -> None:
    meta = export_playlist(settings, conn, ref)
    match_playlist(
        settings, conn, meta["id"], limit=match_limit, delay=delay
    )
    sync_playlist(
        settings, conn, meta["id"], name=name, privacy=privacy, delay=delay
    )

