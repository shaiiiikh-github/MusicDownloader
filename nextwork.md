CONTEXT
Continue working on the YouTube Audio Downloader (app.py, CustomTkinter,
yt-dlp, FFmpeg). Read the current code first. Keep edits targeted; do not
rewrite working code or remove features. Keep it lightweight.

TASK A - FAST DOWNLOADS
1. yt-dlp speed options: concurrent_fragment_downloads=8, retries and
   fragment_retries=10, http_chunk_size=10M. Do not use throttled formats
   when a faster one with the same quality exists.
2. If aria2c is found (PATH or bundled next to the app), use it as the
   external downloader with: -x 16 -s 16 -k 1M. If not found, silently fall back
   to the built-in downloader. No errors, no extra setup for the user.
3. Parallel downloads for queues/playlists: a worker pool with a setting
   "Simultaneous downloads" (1-5, default 3). Per-track rows in the UI with
   their own progress plus one overall progress bar ("12 / 40 done").
4. Skip conversion when the chosen format equals the source codec (for
   example, Opus source and Opus output) to avoid pointless re-encoding.
5. Reuse the already-fetched video info for the download instead of
   fetching it again.
6. Skip files that already exist in the output folder, so a re-run of the
   same playlist resumes instead of re-downloading.

TASK B - IMPORT A SPOTIFY PLAYLIST
Add an "Import Spotify Playlist" option next to the URL box (paste a
open.spotify.com/playlist/... link, or use the "Copy playlist link" from the
Spotify app). Flow:

1. Get the track list (title, artists, album, duration) using these methods,
   in order, falling back automatically:
   a. Public embed page: https://open.spotify.com/embed/playlist/<id>.
      Parse the JSON in the page (__NEXT_DATA__ or similar). No login or
      API keys. Works for public playlists only. Test it for real, since
      Spotify changes this. Note that this method may be capped to ~100 tracks.
   b. Optional Spotify Web API using the USER'S OWN client ID with
      Authorization Code + PKCE (no client secret in the app), only if
      the user enters a client ID in Settings. Use the current endpoints
      (GET /playlists/{id}/items), as the Feb 2026 API changes apply.
   c. Manual fallback: "Import from file/paste" accepting a CSV (Exportify
      format) or plain text with one "Artist - Title" per line.
   If a method fails, show a friendly message and move to the next one.
   Never crash.

2. Matching to YouTube. For each track, search with yt-dlp
   (ytsearch5:"<artist> - <title>" audio), then pick the best candidate by
   score:
   - duration within +/-5 seconds of the Spotify duration (strongest signal)
   - prefer official "Topic" channels / "Provided to YouTube by" / the
     artist's channel
   - penalize titles containing: live, cover, remix, karaoke, reaction,
     slowed, sped up, 8d, lyrics video (unless the Spotify title has it)
   If nothing scores above a threshold, mark the track as "No match" instead
   of downloading a wrong song.

3. Before downloading, show a review list (checkbox per track with the matched
   YouTube title and duration) so the user can untick or fix wrong matches.
   A "Download all" button starts the queue from Task A.

4. Output: use the user's chosen format with highest available quality
   (bestaudio). Name files "Artist - Title.ext". Put them in a subfolder
   named after the playlist. Embed Spotify metadata and cover art
   (title, artist, album, track number) when the format supports it.

5. At the end, show a summary: downloaded / skipped / failed / no match,
   with a "Retry failed" button and an "Export failed list" option.

RULES
- Add a small new module (for example spotify_import.py) for the import logic,
  and keep the UI in app.py.
- Network and search work stays off the UI thread.
- Add rate-limit-friendly behaviour (small delay between searches, retries
  with backoff).
- Add tests for the parsing and the scoring functions using sample data.
- Test each step yourself before moving on. At the end, report what you
  tested, what you could not verify, and the changes made.
- Add a short README note: audio comes from YouTube matches, not Spotify,
  and only public playlists work without a client ID.