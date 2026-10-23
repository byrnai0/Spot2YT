<<<<<<< HEAD
# spotify2yt

A local, resumable CLI that copies your Spotify playlists into YouTube Music — built for libraries with thousands of tracks, no subscription, no per-track limits.

It reads playlists through the **official Spotify Web API** and writes playlists through **`ytmusicapi`**, the reverse-engineered YouTube Music client. It does **not** download audio. It creates YouTube Music playlist entries, exactly like TuneMyMusic does.

```
Spotify Web API  ->  SQLite snapshot  ->  YouTube Music search  ->  score + review  ->  ytmusicapi writes
   (read)              (resume/cache)         (1 request/sec)         (CSV for edge cases)      (create/add)
```

---

## Contents

1. [Why this design](#why-this-design)
2. [What you need](#what-you-need)
3. [Install](#install)
4. [Spotify setup](#1-spotify-setup-official-api)
5. [YouTube Music setup](#2-youtube-music-setup-reverse-engineered)
6. [Your first transfer](#3-your-first-transfer)
7. [The review workflow](#4-the-review-workflow-important)
8. [Command reference](#command-reference)
9. [How matching works](#how-matching-works)
10. [Scale, speed and rate limits](#scale-speed-and-rate-limits)
11. [State, resume and re-runs](#state-resume-and-re-runs)
12. [Troubleshooting](#troubleshooting)
13. [Limits and honest caveats](#limits-and-honest-caveats)
14. [Project layout / development](#project-layout--development)

---

## Why this design

| Need | What we use | Reason |
|---|---|---|
| Read Spotify playlists | Official Web API (`spotipy`, PKCE) | Official, legal, paginated. Spotify currently requires a Premium account for API access. |
| Search/write YouTube Music | `ytmusicapi` | The official YouTube Data API charges **100 quota units per search** (10,000/day default) — only ~100 searches per day. Useless at 1,000+ tracks. `ytmusicapi` uses the same private API as the web player. |
| Survive long jobs | SQLite + per-batch checkpoints | A 3,000-track job must survive a crash, a 429, or Ctrl+C without starting over. |
| Match quality | Fuzzy scoring + review CSV | No service is 100%. We auto-accept obvious matches and let you fix the rest in a spreadsheet. |

**About REA (`morluto/rea`):** REA is an agent/MCP harness for reverse-engineering binaries (Ghidra/IDA-style work). It is not a Spotify/YouTube integration tool, and nothing here needs binary reversing. The reverse-engineered part already exists and is maintained: `ytmusicapi`. Using REA here would add a lot of machinery without solving the actual hard problems (matching and rate limits).

---

## What you need

- Windows, macOS or Linux. This machine already has Python 3.13 and the venv.
- A Spotify account with **Premium** for the official Web API (Spotify's current rule), **or** a CSV/JSON export of your playlist (see [No Premium? Import a file](#no-premium-import-a-file)).
- A YouTube Music account.
- ~1 GB free for the database and JSON snapshots (usually far less).

---

## Install

The project lives at `C:\Users\csp\Projects\spotify2yt` and the venv is already created. To reinstall from scratch:

```powershell
cd C:\Users\csp\Projects\spotify2yt
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Run commands with the wrapper (no activation needed):

```powershell
cd C:\Users\csp\Projects\spotify2yt
.\s2y.cmd doctor
```

Or activate the venv and use `s2y` directly:

```powershell
.\.venv\Scripts\Activate.ps1
s2y doctor
```

If PowerShell blocks activation:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Optional: add `C:\Users\csp\Projects\spotify2yt\.venv\Scripts` to your user `PATH`, then `s2y` works from anywhere.

---

## 1. Spotify setup (official API)

> **Premium required:** Spotify's Web API documentation now says a Premium account is required. If your account is free, skip to [No Premium? Import a file](#no-premium-import-a-file); the rest of the tool works identically.

1. Open <https://developer.spotify.com/dashboard> and log in.
2. **Create app**. Name/description can be anything (`s2y`).
3. Under **Redirect URIs**, add this **exactly**:

   ```
   http://127.0.0.1:8888/callback
   ```

4. Select **Web API** access, save.
5. Open the app and copy the **Client ID**. You do **not** need the Client Secret for PKCE login.
6. Put it in `.env`:

   ```powershell
   cd C:\Users\csp\Projects\spotify2yt
   .\s2y.cmd setup        # creates .env from .env.example if missing
   notepad .env
   ```

   ```dotenv
   SPOTIFY_CLIENT_ID=paste_your_client_id_here
   SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
   ```

7. Log in:

   ```powershell
   .\s2y.cmd spotify-login
   ```

   Your browser opens Spotify's consent page. Approve it. The token is cached at
   `C:\Users\csp\.spotify2yt\auth\spotify_token.json` and refreshes itself.

> If Spotify says "redirect_uri: Not matching configuration", the URI in the dashboard and `.env` differ. They must match character-for-character. `127.0.0.1` is not `localhost`.

### No Premium? Import a file

The Spotify API is only used to read metadata. If you cannot use it, export the playlist from any tool and import the file:

- **Exportify** (open source) → CSV.
- **TuneMyMusic** → CSV export.
- **Spotify "Download your data"** → JSON (if your export includes playlist data).
- Any spreadsheet with columns like `Track Name`, `Artist Name(s)`, `Album Name`, `Track Duration (ms)`, `Track URI`/`ISRC`.

```powershell
cd C:\Users\csp\Projects\spotify2yt
.\s2y.cmd import "playlist.csv" --name "My Playlist"
.\s2y.cmd match "My Playlist"
.\s2y.cmd review "My Playlist"
.\s2y.cmd sync "My Playlist"
```

`import` accepts CSV and JSON, detects common column names, and creates a local playlist with an ID like `import:Demo-Import-73301bc1e6`. That ID works everywhere a Spotify ID does. JSON containing multiple playlists needs `--name` to pick one.

This path uses **no Spotify credentials at all** — only the title/artist/album/duration in the file.

---

## 2. YouTube Music setup (reverse-engineered)

You have two options. **Browser headers** are much faster to set up. **OAuth** is more durable but requires a Google Cloud project.

### Option A — browser headers (recommended)

`ytmusicapi` authenticates as your logged-in browser session. The cookie is equivalent to a password, so keep it private.

1. Open <https://music.youtube.com> and make sure you are logged in.
2. Press **F12** → **Network** tab.
3. In the filter box, type `browse`.
4. Click around (Home, a playlist, a song) until a **POST** request to `browse` appears.
5. Right-click that request → **Copy** → **Copy request headers**.
6. Run:

   ```powershell
   cd C:\Users\csp\Projects\spotify2yt
   .\s2y.cmd yt-login --from-clipboard
   ```

   Or paste the headers into a file and run:

   ```powershell
   .\s2y.cmd yt-login --headers-file headers.txt
   ```

   Or just run `.\s2y.cmd yt-login` and paste into the terminal, then press **Ctrl+Z**, **Enter**.

7. The command verifies the session and prints your account name. The file is saved to
   `C:\Users\csp\.spotify2yt\auth\browser.json`.

**Headers expire** after days/weeks, or when you log out. If you see 401/403 or "auth expired", repeat step 5–6.

### Option B — Google OAuth (durable)

Only do this if browser headers keep expiring. It uses Google's **device flow**, so the OAuth client type must be **TVs and Limited Input devices**.

1. Create a Google Cloud project.
2. Enable **YouTube Data API v3**.
3. Configure the **OAuth consent screen** (External), add your Google account as a **test user**.
4. **Credentials** → **Create credentials** → **OAuth client ID** → **TVs and Limited Input devices**.
5. Copy the client ID and secret into `.env`:

   ```dotenv
   S2Y_GOOGLE_CLIENT_ID=...
   S2Y_GOOGLE_CLIENT_SECRET=...
   ```

6. Run:

   ```powershell
   .\s2y.cmd yt-login --oauth
   ```

7. Open the printed URL, enter the code, approve. The refresh token is saved to `browser.json`.

### Check both logins

```powershell
.\s2y.cmd doctor
```

You want `Spotify API works: <name>` and `YouTube Music API works: <name>`.

---

## 3. Your first transfer

Start small. Pick a 20–50 track playlist first, confirm the results, then run the big ones.

```powershell
cd C:\Users\csp\Projects\spotify2yt

# 1. See your Spotify playlists
.\s2y.cmd playlists

# 2. Snapshot one locally (URL, URI, ID, or name all work)
.\s2y.cmd export "My Test Playlist"

# 3. Search + score every track
.\s2y.cmd match "My Test Playlist" --limit 20

# 4. Inspect progress
.\s2y.cmd status "My Test Playlist"

# 5. See what would be added (creates nothing)
.\s2y.cmd sync "My Test Playlist" --dry-run

# 6. Create the YouTube Music playlist and add tracks
.\s2y.cmd sync "My Test Playlist"
```

At the end you get a link:

```
https://music.youtube.com/playlist?list=...
```

The playlist is **PRIVATE** by default. Use `--privacy UNLISTED` or `--privacy PUBLIC` if you want otherwise.

### One-shot mode

For a playlist you trust:

```powershell
.\s2y.cmd run "My Test Playlist"
```

This does `export` → `match` → `sync`. It still stops to tell you about low-confidence rows; they are simply not added until you review them.

---

## 4. The review workflow (important)

Auto-matching gets most mainstream tracks. The rest are remixes, live versions, regional editions, classical works, or songs YouTube Music doesn't have. Guessing silently is how tools like TuneMyMusic put the wrong song in your playlist.

After `match`:

```powershell
.\s2y.cmd review "My Test Playlist"
```

This writes a CSV to `C:\Users\csp\.spotify2yt\exports\review-*.csv` and shows a table. Open it in Excel (save as **CSV UTF-8** if prompted) or VS Code. Each row looks like:

| position | spotify_title | spotify_artists | ... | candidates | choice |
|---|---|---|---|---|---|
| 12 | Blinding Lights | The Weeknd | ... | `1) Blinding Lights — The Weeknd (3:20) [J7p4bzqLvCw] score=0.99 \|\| 2) ...` | |

Fill the **`choice`** column:

- `1` … `5` — pick that candidate.
- A YouTube URL or 11-character video ID — force a specific video.
- `skip` — mark it unmatched on purpose.
- Leave blank — don't touch the row.

Save, then:

```powershell
.\s2y.cmd apply-review "My Test Playlist" "C:\Users\csp\.spotify2yt\exports\review-....csv"
```

Then `sync` again. Rows you fixed are now `manual` and will be added.

### Finding the right video by hand

```powershell
.\s2y.cmd find "artist song" --limit 10
```

Paste the `Video ID` into the review CSV's `choice` column. You can also open YouTube Music, copy the watch URL, and paste that.

> Excel on Windows sometimes saves "CSV (Comma delimited)" as CP-1252 and mangles accents. Use **CSV UTF-8 (Comma delimited)** or edit in VS Code. `apply-review` reads UTF-8 with or without a BOM.

---

## Command reference

All commands accept global `--home DIR` (before the subcommand) to use a different state directory. `--debug` shows tracebacks.

| Command | What it does |
|---|---|
| `s2y setup` | Create `.env` and print the setup guide. |
| `s2y doctor` | Check config, logins, package versions and connectivity. |
| `s2y spotify-login` | Spotify PKCE browser login (cached + auto-refresh). |
| `s2y yt-login [--headers-file F \| --from-clipboard \| --paste \| --oauth]` | Save YouTube Music auth. Old auth is backed up. |
| `s2y playlists [--limit N] [--local]` | List Spotify playlists, or locally exported ones. |
| `s2y export PLAYLIST` | Fetch all tracks into SQLite and write a JSON snapshot. |
| `s2y import FILE [--name NAME]` | Import a CSV/JSON track list; no Spotify API needed. |
| `s2y match PLAYLIST [--limit N] [--force] [--refresh] [--delay S]` | Search YouTube Music, score candidates, store top 5. |
| `s2y review PLAYLIST [--output PATH]` | Write the review CSV. |
| `s2y apply-review PLAYLIST CSV` | Apply your choices. |
| `s2y sync PLAYLIST [--name NAME] [--privacy ...] [--playlist-id ID] [--limit N] [--dry-run] [--new] [--delay S]` | Create/reuse the YouTube playlist and add matched tracks. |
| `s2y status PLAYLIST` | Counts by status, link, and the worst rows. |
| `s2y find QUERY [--songs] [--limit N]` | Test a YouTube Music search (works without auth). |
| `s2y run PLAYLIST [--limit N] [--privacy ...] [--name NAME]` | `export` + `match` + `sync`. |

Useful flags:

- `match --force` — re-check everything except already-added tracks (after changing thresholds or fixing a bad cache).
- `match --refresh` — ignore cached YouTube search results.
- `sync --dry-run` — show the first 20 tracks that would be added, create nothing.
- `sync --new` — create a fresh playlist even if one with the same name exists. The new ID is stored, so future syncs target it.
- `sync --playlist-id ID` — add to a specific existing YouTube Music playlist.

Playlist arguments accept:

- `https://open.spotify.com/playlist/37i9...?si=...`
- `spotify:playlist:37i9...`
- `37i9dQZF1DXcBWIGoYBM5M`
- A name, matched exactly first, then fuzzily (≥80%). Ambiguous names tell you to use the ID.

---

## How matching works

For each Spotify track:

1. **Query**: `"<title> <primary artist>"`, searching YouTube Music's `songs` filter. If there are zero results, it retries without the filter (music videos/user uploads).
2. **Normalize**: lowercase, strip accents and punctuation, remove `feat.` tails, remaster/deluxe/official-video noise.
3. **Score** each of the top results:

   ```
   0.55 * title similarity
 + 0.30 * artist similarity
 + 0.15 * duration similarity
 + bonus (official album track, album match, result type)
 - version penalties (karaoke, cover, live, remix, instrumental, sped-up)
   ```

4. **Auto-accept** only when *all* of these hold:
   - total score ≥ `S2Y_AUTO_THRESHOLD` (default 0.80)
   - title ≥ 0.82
   - artist ≥ 0.70
   - duration ≥ 0.45 (within ~20 seconds)
   - no hard version mismatch (live/remix/karaoke/cover/instrumental/sped-up/jazz-lofi-piano variants)

5. Anything ≥ `S2Y_REVIEW_THRESHOLD` (default 0.50) becomes **review**; the rest **unmatched**. The top 5 candidates are always stored, so the review CSV is useful even for unmatched rows.

Live/remix/acoustic matches are deliberately sent to review even when the text looks identical, because they are different recordings.

### Tuning

Set in `.env`:

```dotenv
S2Y_AUTO_THRESHOLD=0.80     # lower = more automatic, more mistakes
S2Y_REVIEW_THRESHOLD=0.50   # below this = unmatched
S2Y_SEARCH_LIMIT=8          # candidates fetched per track
S2Y_DELAY=1.0               # seconds between YouTube requests
S2Y_BATCH_SIZE=20           # videos per add request
S2Y_CACHE_TTL_DAYS=30       # reuse search results
```

After changing thresholds:

```powershell
.\s2y.cmd match "Playlist" --force
```

---

## Scale, speed and rate limits

- **YouTube Music** has no published quota, but it rate-limits. The default is **1 request/second** plus jitter, with exponential backoff on 429/5xx. At ~1.2 s per search, 1,000 tracks take roughly **20–35 minutes**; 5,000 tracks take **2–3 hours**. Search results are cached, so reruns and duplicate songs are much faster.
- **Spotify** export is fast (100 items per page). Spotify 429s are retried by `spotipy`.
- **Sync** adds 20 videos per request. 1,000 tracks is roughly 50 requests, ~2–5 minutes.
- Lower `S2Y_DELAY` to `0.5` only if you are impatient and willing to handle 429s. Do not hammer it.
- Everything is checkpointed: matching commits per track, sync per batch. Ctrl+C is safe.

**Estimating:** `playlist size × 1.2 s` for the first match, plus review time. The bottleneck is your review, not the API.

---

## State, resume and re-runs

Default state directory: `C:\Users\csp\.spotify2yt`

```
state.db                         SQLite: playlists, tracks, matches, search cache
auth/spotify_token.json          Spotify refresh token
auth/browser.json                YouTube Music cookie/OAuth token
exports/*.json                   Spotify snapshots (audit / re-import)
exports/review-*.csv             Review files
```

Common operations:

```powershell
# Resume after Ctrl+C or a crash: just rerun the same command
.\s2y.cmd match "Playlist"

# Playlist changed on Spotify: re-export, then match only new/unmatched tracks
.\s2y.cmd export "Playlist"
.\s2y.cmd match "Playlist"
.\s2y.cmd sync "Playlist"

# Force a full re-match (does not touch already-added tracks)
.\s2y.cmd match "Playlist" --force

# Start a playlist over from scratch (keeps auth)
Remove-Item "$env:USERPROFILE\.spotify2yt\state.db"
```

`export` preserves matches for tracks that stay at the same position. If a position now holds a different Spotify track, its match is reset to `pending`.

`sync` reconciles against the actual YouTube playlist before adding, so a crash mid-batch won't create duplicates. It never deletes tracks from YouTube Music; removing tracks is a manual decision.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `SPOTIFY_CLIENT_ID is not set` | Run `s2y setup`, edit `.env`. |
| Spotify redirect mismatch | Dashboard URI must be exactly `http://127.0.0.1:8888/callback`. |
| Spotify login window never returns | Port 8888 is busy. Change the URI in the dashboard **and** `.env` to another port, then delete `auth/spotify_token.json` and retry. |
| `YouTube Music is not authenticated` | Run `s2y yt-login --from-clipboard`. |
| 401/403 from YouTube Music | Headers expired. Re-copy them while logged in. |
| 429 / "Too Many Requests" | Let the built-in backoff work; raise `S2Y_DELAY` to `1.5` or `2.0`. |
| `Could not find "browse" request` | Filter Network by `browse`, then click a playlist/song to force a POST. Copy **request** headers, not response headers. |
| Playlist not found after sync | It was deleted in YouTube Music. `sync` detects this and creates/reuses another. |
| Review CSV shows `????` | Open/save as **CSV UTF-8**, or edit in VS Code. |
| A track is "unmatched" but exists | Search it with `s2y find`, copy the video ID into the review CSV, `apply-review`, `sync`. |
| Wrong version was auto-added | `sync` never removes; edit the YouTube playlist manually. To prevent it next time, lower `S2Y_AUTO_THRESHOLD` or improve review discipline. |
| YouTube playlist has duplicates | YouTube Music generally rejects duplicate videos; `sync` also dedupes by video ID. |
| `database is locked` | Another `s2y` process is running. Close it and rerun. |

---

## Limits and honest caveats

- **No audio downloading.** This creates playlist entries only. Downloading Spotify audio would violate Spotify's terms and is a different (much more legally fraught) project.
- **YouTube Music's private API is unofficial.** Automating playlist writes may violate YouTube's Terms of Service. Use your own account, keep rates modest, and accept that Google can change or block the API at any time. If you care about the account, use a secondary one.
- **Auth files are passwords.** `browser.json` contains your YouTube cookies; `spotify_token.json` contains a refresh token. Never commit, paste, or share them. `.gitignore` already excludes them from this repo.
- **Catalog gaps are real.** Some Spotify tracks are not on YouTube Music in your region, or only exist as live/cover/user uploads. The review step is not optional if you care about accuracy.
- **No two-way sync.** Changes made on YouTube Music are not pushed back to Spotify, and deleting from Spotify does not delete from YouTube. `sync` only adds.
- **Duplicates can't exist in a YouTube Music playlist.** If a Spotify playlist has the same song twice, only one copy is added; the second row is marked `duplicate`.
- **Local files and podcast episodes are skipped** during export.
- **Order**: matched tracks are added in Spotify order. Unmatched tracks leave gaps; they do not shift the rest.
- **Classical / multi-artist / non-Latin metadata** will need more review. Duration and artist scoring help, but no fuzzy matcher is perfect.

---

## Project layout / development

```
spotify2yt/
  pyproject.toml
  .env.example
  s2y.cmd                     Windows launcher (uses .venv)
  spotify2yt/
    cli.py                    argparse commands + error handling
    commands.py               export / match / review / sync workflows
    matching.py               normalization, scoring, version detection
    spotify_api.py            PKCE auth, playlist resolution, pagination
    importer.py               CSV/JSON track-list import (no Spotify API)
    youtube_api.py            ytmusicapi wrapper: throttle, retry, batch writes
    store.py                  SQLite queries and state transitions
    db.py                     schema
    config.py                 .env + paths + thresholds
    util.py                   IDs, durations, slugs
  tests/                      unittest suite (matching, store, refs)
```

Run tests:

```powershell
cd C:\Users\csp\Projects\spotify2yt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests cover normalization/version detection, scoring edge cases (karaoke, live, wrong duration, wrong artist, jazz variants), ranking, ref parsing, CSV/JSON import, and DB resume semantics.

### Extending it

- **Better matching**: add MusicBrainz/ISRC lookup for classical and obscure tracks; Spotify provides `external_ids.isrc`, already stored.
- **CSV import**: `apply-review` is intentionally simple; add a `--format json` if you prefer.
- **Multiple playlists**: loop `export`/`match`/`sync`; state is keyed by Spotify playlist ID.
- **Web UI**: the SQLite schema is the API. A small local web app could read `matches` and write manual choices.

---

## Safety checklist

- [ ] `.env` and `auth/` are not in git (they aren't by default).
- [ ] Start with a small playlist and `sync --dry-run`.
- [ ] Keep `S2Y_DELAY` at 1.0 unless you know what you're doing.
- [ ] Review low-confidence rows before adding 1,000 tracks.
- [ ] Back up the YouTube playlist link after the first successful sync.

Happy transferring.
=======
# Spot2YT
>>>>>>> 1efdb92034374cc8c670414148dc2d88cc285977
