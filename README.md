# YouTube Audio Downloader & Spotify Playlist Importer

A lightweight desktop audio downloader built with Python, CustomTkinter, `yt-dlp`, and FFmpeg.

> [!NOTE]
> **Audio Source & Spotify Playlists:**
> Audio is downloaded from matched YouTube streams, not directly from Spotify. Only public Spotify playlists work without a client ID (via Spotify's public embed page). For private playlists or API-based imports, enter your own Spotify Client ID in Settings (uses PKCE authorization without storing any client secret).

---

## Features

### Fast Downloads (Task A)
- **High-speed multi-fragment fetching**: `concurrent_fragment_downloads = 8`, `retries = 10`, `fragment_retries = 10`, and `http_chunk_size = 10M`.
- **Aria2c integration**: Automatically detects `aria2c` in system `PATH` or bundled next to the app (`-x 16 -s 16 -k 1M`). Silently falls back to built-in downloader if not installed.
- **Transcoding bypass**: Skips re-encoding when chosen format matches source codec (e.g., Opus source to Opus output).
- **Metadata reuse**: Reuses pre-fetched video info on download instead of fetching it twice.
- **Smart resume & file skip**: Detects existing completed tracks in destination folder and skips them.
- **Multi-threaded worker pool**: Simultaneous downloads queue (1–5 parallel workers, default 3) with per-track and overall progress bars.

### Spotify Playlist Import (Task B)
- **Automatic Fallback Extraction**:
  1. **Public Embed Page**: Scrapes `https://open.spotify.com/embed/playlist/<id>` via `__NEXT_DATA__` (zero login or API keys required, works for public playlists).
  2. **Spotify Web API + PKCE**: User can supply their own Spotify Client ID in Settings (Authorization Code with PKCE, compliant with modern Feb 2026 `/items` endpoints).
  3. **Manual Fallback**: Import from Exportify CSV or plain text (`Artist - Title` per line).
- **Intelligent YouTube Matching & Scoring**:
  - Duration difference within ±5 seconds (strongest signal)
  - Prioritizes official Topic channels, `"Provided to YouTube by"`, and verified artist channels
  - Penalizes covers, live recordings, slowed, sped up, 8D audio, and karaoke versions (unless explicitly present in the original track title)
  - Tracks below score threshold marked as "No match"
- **Interactive Review Screen**:
  - Review all matched tracks with match score badges and durations
  - Edit or replace matched YouTube URLs manually before downloading
  - Checkboxes to select / deselect individual tracks
- **Output & Tagging**:
  - High quality `bestaudio` in chosen format (MP3, M4A, Opus, FLAC, WAV, Best)
  - Output files named `Artist - Title.ext` inside a subfolder named after the playlist
  - Spotify metadata (Title, Artist, Album, Track Number) and cover art embedded automatically
- **Summary & Recovery**:
  - End-of-download report (Downloaded, Skipped, Failed, No match)
  - "Retry Failed" button
  - "Export Failed List" to CSV

---

## Installation & Running

1. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **FFmpeg & Aria2c**:
   - Ensure `ffmpeg` is installed and on your PATH for audio conversion and metadata embedding.
   - (Optional) Install `aria2c` for maximum multi-connection download speeds.

3. **Run the App**:
   ```bash
   python app.py
   # or
   python music.py
   ```

4. **Run Unit Tests**:
   ```bash
   pytest -v test_spotify_import.py
   ```

