"""Spotify Playlist Import and YouTube Matching Module.

Handles:
- Spotify playlist extraction (Public Embed page, Spotify Web API with PKCE, CSV/Text fallback)
- Intelligent YouTube candidate matching and scoring
- Fast yt-dlp configuration and aria2c integration
- Rate-limited searching with backoff
- Exporting failed/unmatched track reports
"""

import base64
import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Penalized keywords unless they are part of the original track title
PENALIZED_KEYWORDS = [
    "live", "cover", "remix", "karaoke", "reaction",
    "slowed", "sped up", "8d", "lyrics", "lyric video", "acoustic"
]


@dataclass
class SpotifyTrack:
    id: str
    title: str
    artists: str
    album: str = ""
    duration_ms: int = 0
    track_number: int = 1
    cover_url: str = ""
    # YouTube matched fields
    matched_id: str = ""
    matched_title: str = ""
    matched_url: str = ""
    matched_duration: int = 0
    matched_channel: str = ""
    score: float = 0.0
    selected: bool = True
    status: str = "Pending"  # Pending, Downloading, Done, Skipped, Failed, No match, Already Downloaded
    error_message: str = ""
    is_duplicate: bool = False
    duplicate_info: str = ""

    @property
    def duration_sec(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def safe_filename_base(self) -> str:
        clean_artist = sanitize_filename(self.artists)
        clean_title = sanitize_filename(self.title)
        return f"{clean_artist} - {clean_title}"


def sanitize_filename(name: str) -> str:
    """Sanitize a string for safe filesystem usage across platforms."""
    if not name:
        return "Unknown"
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:150] or "Unknown"


# ==============================================================================
# DOWNLOAD HISTORY & DEDUPLICATION MANAGER
# ==============================================================================

class DownloadHistoryManager:
    """Tracks downloaded songs across YouTube and Spotify to avoid duplicate downloads."""

    def __init__(self, history_file: Optional[Path] = None):
        if history_file is None:
            self.history_file = Path(__file__).resolve().parent / "download_history.json"
        else:
            self.history_file = Path(history_file)
        self.data = self._load()

    def _load(self) -> dict:
        if self.history_file.is_file():
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "spotify_ids": {},
            "youtube_ids": {},
            "normalized_keys": {},
        }

    def save(self):
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception:
            pass

    @staticmethod
    def normalize_key(artist: str = "", title: str = "") -> str:
        """Create a normalized key to match titles despite punctuation, tags, or remixes."""
        t = re.sub(r"\(.*?\)|\[.*?\]", "", title or "").strip().lower()
        a = re.sub(r"\(.*?\)|\[.*?\]", "", artist or "").strip().lower()

        # Remove common trailing tags like - Remastered 2011, - Official Audio, etc.
        t = re.sub(
            r"-\s*(remastered|remaster|live|official|audio|video|lyrics|hd|hq).*",
            "",
            t,
            flags=re.IGNORECASE,
        ).strip()

        norm_a = re.sub(r"[^a-z0-9]", "", a)
        norm_t = re.sub(r"[^a-z0-9]", "", t)

        if norm_a and norm_a not in ("unknown", "unknownartist"):
            return f"{norm_a}:{norm_t}"
        return norm_t

    def record_download(
        self,
        title: str,
        artist: str = "",
        spotify_id: str = "",
        youtube_id: str = "",
        file_path: str = "",
    ):
        """Record a completed download into history."""
        record = {
            "title": title,
            "artist": artist,
            "spotify_id": spotify_id,
            "youtube_id": youtube_id,
            "file_path": str(file_path),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if spotify_id:
            self.data.setdefault("spotify_ids", {})[spotify_id] = record
        if youtube_id:
            self.data.setdefault("youtube_ids", {})[youtube_id] = record

        norm_key = self.normalize_key(artist, title)
        if norm_key:
            self.data.setdefault("normalized_keys", {})[norm_key] = record

        # Also store with title alone if artist was combined
        if " - " in title and not artist:
            parts = title.split(" - ", 1)
            k2 = self.normalize_key(parts[0], parts[1])
            if k2:
                self.data.setdefault("normalized_keys", {})[k2] = record

        self.save()

    def is_downloaded(
        self,
        title: str = "",
        artist: str = "",
        spotify_id: str = "",
        youtube_id: str = "",
    ) -> tuple[bool, Optional[dict]]:
        """Check if track has already been downloaded by spotify_id, youtube_id, or normalized title."""
        # 1. Match by Spotify ID
        if spotify_id and spotify_id in self.data.get("spotify_ids", {}):
            return True, self.data["spotify_ids"][spotify_id]

        # 2. Match by YouTube Video ID
        if youtube_id and youtube_id in self.data.get("youtube_ids", {}):
            return True, self.data["youtube_ids"][youtube_id]

        # 3. Match by normalized key
        norm_key = self.normalize_key(artist, title)
        if norm_key and norm_key in self.data.get("normalized_keys", {}):
            return True, self.data["normalized_keys"][norm_key]

        # 4. Try parsing "Artist - Title" if artist is empty
        if not artist and " - " in title:
            parts = title.split(" - ", 1)
            k2 = self.normalize_key(parts[0], parts[1])
            if k2 and k2 in self.data.get("normalized_keys", {}):
                return True, self.data["normalized_keys"][k2]

        return False, None

    def scan_and_index_folder(self, folder: Path) -> int:
        """Scan a folder for existing audio files and index them into history."""
        if not folder.is_dir():
            return 0
        audio_exts = {".mp3", ".m4a", ".opus", ".flac", ".wav", ".webm", ".ogg"}
        added = 0
        try:
            for item in folder.rglob("*"):
                if item.is_file() and item.suffix.lower() in audio_exts and item.stat().st_size > 1024:
                    stem = item.stem
                    if " - " in stem:
                        a, t = stem.split(" - ", 1)
                    else:
                        a, t = "", stem
                    norm_key = self.normalize_key(a, t)
                    if norm_key and norm_key not in self.data.get("normalized_keys", {}):
                        self.data.setdefault("normalized_keys", {})[norm_key] = {
                            "title": t.strip(),
                            "artist": a.strip(),
                            "file_path": str(item),
                            "timestamp": time.strftime(
                                "%Y-%m-%d %H:%M:%S", time.localtime(item.stat().st_mtime)
                            ),
                        }
                        added += 1
            if added > 0:
                self.save()
        except Exception:
            pass
        return added

    def clear(self):
        """Clear all download history."""
        self.data = {"spotify_ids": {}, "youtube_ids": {}, "normalized_keys": {}}
        self.save()

    def count(self) -> int:
        """Return total unique tracks in history."""
        keys = set(self.data.get("normalized_keys", {}).keys())
        keys.update(self.data.get("spotify_ids", {}).keys())
        keys.update(self.data.get("youtube_ids", {}).keys())
        return len(keys)


_GLOBAL_HISTORY: Optional[DownloadHistoryManager] = None


def get_download_history(history_file: Optional[Path] = None) -> DownloadHistoryManager:
    """Get the global singleton DownloadHistoryManager instance."""
    global _GLOBAL_HISTORY
    if _GLOBAL_HISTORY is None or history_file is not None:
        _GLOBAL_HISTORY = DownloadHistoryManager(history_file)
    return _GLOBAL_HISTORY


# ==============================================================================
# SPEED & DOWNLOAD CONFIGURATION (TASK A)
# ==============================================================================

def get_aria2c_path() -> Optional[str]:
    """Check PATH or bundled directory next to app for aria2c binary."""
    found = shutil.which("aria2c")
    if found:
        return found
    app_dir = Path(__file__).resolve().parent
    for name in ("aria2c.exe", "aria2c"):
        candidate = app_dir / name
        if candidate.is_file():
            return str(candidate)
    return None


def should_skip_conversion(source_acodec: Optional[str], target_codec: Optional[str]) -> bool:
    """Return True if the source audio codec matches target codec to avoid transcoding."""
    if not target_codec or target_codec == "best":
        return True
    if not source_acodec or source_acodec.lower() == "none":
        return False

    src = source_acodec.lower()
    tgt = target_codec.lower()

    if tgt == "opus" and "opus" in src:
        return True
    if tgt == "m4a" and ("aac" in src or "m4a" in src or "mp4a" in src):
        return True
    if tgt == "mp3" and "mp3" in src:
        return True
    if tgt == "flac" and "flac" in src:
        return True
    if tgt == "wav" and "wav" in src:
        return True
    return tgt == src


def build_fast_ytdl_opts(
    outtmpl: str,
    target_codec: Optional[str] = None,
    progress_hook: Optional[Callable] = None,
    source_acodec: Optional[str] = None,
    embed_metadata: bool = True,
    embed_thumbnail: bool = True,
    no_overwrites: bool = True,
    track_metadata: Optional[dict] = None,
    has_ffmpeg: bool = True,
) -> dict:
    """Construct yt-dlp options optimized for speed, aria2c, retries, and format handling."""
    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Task A speed options
        "concurrent_fragment_downloads": 8,
        "retries": 10,
        "fragment_retries": 10,
        "http_chunk_size": 10 * 1024 * 1024,  # 10M chunk size
        "nooverwrites": no_overwrites,
        "overwrites": not no_overwrites,
    }

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    # Aria2c external downloader integration (Task A.2)
    aria2c_bin = get_aria2c_path()
    if aria2c_bin:
        opts["external_downloader"] = {"default": aria2c_bin}
        opts["external_downloader_args"] = {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M"]
        }

    postprocessors = []

    # Check if conversion can be skipped (Task A.4)
    skip_conversion = should_skip_conversion(source_acodec, target_codec)

    if target_codec and not skip_conversion and has_ffmpeg:
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": target_codec,
            "preferredquality": "0",  # Highest VBR quality
        })

    if embed_metadata and has_ffmpeg:
        postprocessors.append({
            "key": "FFmpegMetadata",
            "add_metadata": True,
        })
        if track_metadata:
            meta_args = []
            if track_metadata.get("title"):
                meta_args.extend(["-metadata", f"title={track_metadata['title']}"])
            if track_metadata.get("artist"):
                meta_args.extend(["-metadata", f"artist={track_metadata['artist']}"])
            if track_metadata.get("album"):
                meta_args.extend(["-metadata", f"album={track_metadata['album']}"])
            if track_metadata.get("track_number"):
                meta_args.extend(["-metadata", f"track={track_metadata['track_number']}"])
            if meta_args:
                opts.setdefault("postprocessor_args", {})["FFmpegMetadata"] = meta_args

    if embed_thumbnail and has_ffmpeg:
        opts["writethumbnail"] = True
        postprocessors.append({
            "key": "EmbedThumbnail",
            "already_have_thumbnail": False,
        })

    if postprocessors:
        opts["postprocessors"] = postprocessors

    return opts


def track_already_exists(output_dir: Path, filename_base: str, target_codec: Optional[str]) -> Optional[Path]:
    """Check if the audio file already exists in destination folder (Task A.6)."""
    if not output_dir.exists():
        return None

    ext_map = {
        "mp3": [".mp3"],
        "m4a": [".m4a", ".aac"],
        "opus": [".opus", ".webm", ".ogg"],
        "flac": [".flac"],
        "wav": [".wav"],
        None: [".mp3", ".m4a", ".opus", ".webm", ".flac", ".wav", ".ogg"],
    }
    candidates = ext_map.get(target_codec.lower() if target_codec else None, [f".{target_codec}"])
    for ext in candidates:
        p = output_dir / f"{filename_base}{ext}"
        if p.is_file() and p.stat().st_size > 1024:
            return p
    return None


# ==============================================================================
# SPOTIFY PLAYLIST EXTRACTION (TASK B.1)
# ==============================================================================

def extract_spotify_playlist_id(url_or_uri: str) -> Optional[str]:
    """Extract 22-character Spotify playlist ID from URL or URI."""
    if not url_or_uri:
        return None
    s = url_or_uri.strip()
    m = re.search(r"spotify:playlist:([a-zA-Z0-9]{22})", s)
    if m:
        return m.group(1)
    m = re.search(r"open\.spotify\.com/(?:embed/)?playlist/([a-zA-Z0-9]{22})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9]{22}", s):
        return s
    return None


def fetch_playlist_embed(playlist_id: str) -> tuple[str, str, list[SpotifyTrack]]:
    """Method A: Fetch public Spotify playlist embed page and parse __NEXT_DATA__."""
    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    req = urllib.request.Request(
        embed_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    match = re.search(r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not match:
        raise ValueError("Could not find __NEXT_DATA__ in Spotify embed HTML")

    payload = json.loads(match.group(1))
    return parse_embed_json(payload)


def parse_embed_json(payload: dict) -> tuple[str, str, list[SpotifyTrack]]:
    """Parse tracks and metadata from __NEXT_DATA__ JSON payload."""
    page_props = payload.get("props", {}).get("pageProps", {})
    state = page_props.get("state", {})
    data = state.get("data", {})
    entity = data.get("entity", {})

    if not entity:
        def find_entity(d):
            if isinstance(d, dict):
                if "trackList" in d:
                    return d
                for v in d.values():
                    found = find_entity(v)
                    if found:
                        return found
            return None
        entity = find_entity(payload) or {}

    playlist_name = entity.get("title") or entity.get("name") or "Spotify Playlist"
    cover_url = ""

    visual = entity.get("visualIdentity", {})
    images = visual.get("image", [])
    if images and isinstance(images, list):
        sorted_imgs = sorted(images, key=lambda x: x.get("maxWidth", 0), reverse=True)
        cover_url = sorted_imgs[0].get("url", "")
    if not cover_url and entity.get("coverArt"):
        cover_url = entity["coverArt"].get("sources", [{}])[0].get("url", "")

    raw_tracks = entity.get("trackList", [])
    if not raw_tracks:
        raise ValueError("No tracks found in Spotify embed trackList")

    tracks: list[SpotifyTrack] = []
    for idx, t in enumerate(raw_tracks, start=1):
        if not isinstance(t, dict):
            continue
        title = t.get("title") or t.get("name") or "Unknown"
        artists = t.get("subtitle") or ""
        if not artists and t.get("artists"):
            if isinstance(t["artists"], list):
                artists = ", ".join(a.get("name", "") for a in t["artists"] if isinstance(a, dict))
        if not artists:
            artists = "Unknown Artist"

        duration_ms = t.get("duration") or 0
        uri = t.get("uri", "")
        track_id = uri.split(":")[-1] if uri else f"track_{idx}"

        tracks.append(
            SpotifyTrack(
                id=track_id,
                title=title,
                artists=artists,
                album=playlist_name,
                duration_ms=int(duration_ms),
                track_number=idx,
                cover_url=cover_url,
            )
        )

    return playlist_name, cover_url, tracks


# ==============================================================================
# SPOTIFY WEB API + PKCE (TASK B.1.b)
# ==============================================================================

class SpotifyPKCEAuth:
    """Helper for Spotify Authorization Code Flow with PKCE."""

    REDIRECT_URI = "http://127.0.0.1:8888/callback"

    @classmethod
    def generate_pkce(cls) -> tuple[str, str]:
        verifier = secrets.token_urlsafe(64)[:96]
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
        return verifier, challenge

    @classmethod
    def build_authorize_url(cls, client_id: str, code_challenge: str) -> str:
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": cls.REDIRECT_URI,
            "code_challenge_method": "S256",
            "code_challenge": code_challenge,
            "scope": "playlist-read-private playlist-read-collaborative",
        }
        return f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}"

    @classmethod
    def exchange_code_for_token(cls, client_id: str, code: str, code_verifier: str) -> dict:
        data = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": cls.REDIRECT_URI,
            "code_verifier": code_verifier,
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))


def fetch_playlist_web_api(playlist_id: str, access_token: str) -> tuple[str, str, list[SpotifyTrack]]:
    """Method B: Fetch playlist using Spotify Web API endpoints (Feb 2026 items endpoint)."""
    headers = {"Authorization": f"Bearer {access_token}"}

    meta_url = f"https://api.spotify.com/v1/playlists/{playlist_id}?fields=name,images"
    req_meta = urllib.request.Request(meta_url, headers=headers)
    with urllib.request.urlopen(req_meta, timeout=15) as resp:
        meta_data = json.loads(resp.read().decode("utf-8"))

    playlist_name = meta_data.get("name", "Spotify Playlist")
    cover_url = ""
    images = meta_data.get("images", [])
    if images and isinstance(images, list):
        cover_url = images[0].get("url", "")

    tracks: list[SpotifyTrack] = []
    limit = 50
    offset = 0

    while True:
        items_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/items?limit={limit}&offset={offset}"
        req_items = urllib.request.Request(items_url, headers=headers)
        with urllib.request.urlopen(req_items, timeout=15) as resp:
            page = json.loads(resp.read().decode("utf-8"))

        items = page.get("items", [])
        if not items:
            break

        for item in items:
            t = item.get("track") or item.get("item")
            if not t or not isinstance(t, dict):
                continue

            track_id = t.get("id") or str(len(tracks) + 1)
            title = t.get("name", "Unknown Title")
            artists_list = t.get("artists", [])
            artists = ", ".join(a.get("name", "") for a in artists_list if isinstance(a, dict))
            album_info = t.get("album", {})
            album = album_info.get("name", playlist_name)
            duration_ms = t.get("duration_ms", 0)
            track_num = t.get("track_number", len(tracks) + 1)

            t_cover = cover_url
            if album_info.get("images"):
                t_cover = album_info["images"][0].get("url", cover_url)

            tracks.append(
                SpotifyTrack(
                    id=track_id,
                    title=title,
                    artists=artists or "Unknown Artist",
                    album=album,
                    duration_ms=int(duration_ms),
                    track_number=int(track_num),
                    cover_url=t_cover,
                )
            )

        if not page.get("next"):
            break
        offset += limit

    if not tracks:
        raise ValueError("Spotify Web API returned 0 tracks for this playlist")

    return playlist_name, cover_url, tracks


# ==============================================================================
# MANUAL FALLBACK: CSV / TEXT (TASK B.1.c)
# ==============================================================================

def parse_exportify_csv(csv_text: str) -> list[SpotifyTrack]:
    """Parse CSV text in Exportify format or generic track CSV."""
    f = io.StringIO(csv_text.strip())
    reader = csv.DictReader(f)
    if not reader.fieldnames:
        return []

    headers = {h.strip().lower(): h for h in reader.fieldnames if h}

    track_col = next((headers[k] for k in headers if k in ("track name", "track", "title", "song name")), None)
    artist_col = next((headers[k] for k in headers if k in ("artist name(s)", "artist name", "artist", "artists")), None)
    album_col = next((headers[k] for k in headers if k in ("album name", "album")), None)
    dur_col = next((headers[k] for k in headers if k in ("track duration (ms)", "duration (ms)", "duration", "ms")), None)
    id_col = next((headers[k] for k in headers if k in ("spotify id", "track uri", "id")), None)

    if not track_col:
        return []

    tracks: list[SpotifyTrack] = []
    for idx, row in enumerate(reader, start=1):
        title = (row.get(track_col) or "").strip()
        if not title:
            continue
        artists = (row.get(artist_col) if artist_col else "Unknown Artist") or "Unknown Artist"
        album = (row.get(album_col) if album_col else "") or ""
        duration_ms = 0
        if dur_col and row.get(dur_col):
            try:
                duration_ms = int(float(row[dur_col]))
            except ValueError:
                pass
        t_id = (row.get(id_col) if id_col else f"csv_{idx}") or f"csv_{idx}"

        tracks.append(
            SpotifyTrack(
                id=t_id,
                title=title,
                artists=artists,
                album=album,
                duration_ms=duration_ms,
                track_number=idx,
            )
        )

    return tracks


def parse_plain_text_tracks(text: str) -> list[SpotifyTrack]:
    """Parse plain text with one 'Artist - Title' per line."""
    tracks: list[SpotifyTrack] = []
    lines = text.strip().splitlines()
    for idx, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if " - " in line:
            parts = line.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()
        else:
            artist = "Unknown Artist"
            title = line

        tracks.append(
            SpotifyTrack(
                id=f"text_{idx}",
                title=title,
                artists=artist,
                duration_ms=0,
                track_number=idx,
            )
        )
    return tracks


# ==============================================================================
# MATCHING & SCORING (TASK B.2)
# ==============================================================================

def score_match(track: SpotifyTrack, candidate: dict) -> float:
    """Score a YouTube search candidate against a Spotify track."""
    score = 100.0

    cand_title = candidate.get("title", "").lower()
    cand_uploader = candidate.get("uploader", "").lower()
    cand_channel = candidate.get("channel", "").lower()
    cand_desc = (candidate.get("description", "") or "").lower()
    cand_duration = candidate.get("duration") or 0

    target_duration = track.duration_sec
    track_title_lower = track.title.lower()
    track_artist_lower = track.artists.lower()

    # 1. Duration check (+/- 5s strongest signal)
    if target_duration > 0 and cand_duration > 0:
        diff = abs(target_duration - cand_duration)
        if diff <= 2.5:
            score += 35.0
        elif diff <= 5.0:
            score += 25.0
        elif diff <= 10.0:
            score += 10.0
        elif diff <= 20.0:
            score -= 20.0
        elif diff <= 45.0:
            score -= 50.0
        else:
            score -= 85.0

    # 2. Prefer Topic channel / Provided to YouTube / Artist channel
    is_topic = " - topic" in cand_uploader or " - topic" in cand_channel
    is_provided = "provided to youtube by" in cand_desc
    is_artist_channel = track_artist_lower in cand_uploader or track_artist_lower in cand_channel

    if is_topic:
        score += 30.0
    if is_provided:
        score += 25.0
    if is_artist_channel:
        score += 20.0

    # 3. Penalize unwanted variations unless in original track title
    for term in PENALIZED_KEYWORDS:
        if term in cand_title and term not in track_title_lower:
            score -= 30.0

    # 4. Check clean title matching
    clean_target = re.sub(r"\(.*?\)|\[.*?\]", "", track_title_lower).strip()
    if clean_target and clean_target in cand_title:
        score += 20.0
    elif clean_target:
        words = [w for w in re.findall(r"\w+", clean_target) if len(w) > 2]
        if words:
            matched_words = sum(1 for w in words if w in cand_title)
            ratio = matched_words / len(words)
            if ratio < 0.4:
                score -= 30.0

    return max(0.0, score)


def match_track_to_youtube(
    track: SpotifyTrack,
    ydl_instance,
    min_score_threshold: float = 60.0,
    delay_between_searches: float = 0.5,
    max_retries: int = 3,
) -> tuple[Optional[dict], float]:
    """Search YouTube for the track and find the highest scoring candidate."""
    query = f"ytsearch5:{track.artists} - {track.title} audio"

    entries = None
    for attempt in range(max_retries):
        try:
            res = ydl_instance.extract_info(query, download=False)
            if res and "entries" in res:
                entries = [e for e in res["entries"] if e]
            break
        except Exception:
            time.sleep(1.0 * (attempt + 1))

    if delay_between_searches > 0:
        time.sleep(delay_between_searches)

    if not entries:
        return None, 0.0

    best_cand = None
    best_score = -1.0

    for cand in entries:
        sc = score_match(track, cand)
        if sc > best_score:
            best_score = sc
            best_cand = cand

    if best_score < min_score_threshold:
        return None, best_score

    return best_cand, best_score


# ==============================================================================
# EXPORT REPORTS (TASK B.5)
# ==============================================================================

def export_failed_tracks_csv(tracks: list[SpotifyTrack], out_file: Path):
    """Export failed and unmatched tracks to a CSV file."""
    failed = [t for t in tracks if t.status in ("Failed", "No match")]
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Spotify ID", "Artist", "Title", "Album", "Status", "Error / Reason"])
        for t in failed:
            writer.writerow([t.id, t.artists, t.title, t.album, t.status, t.error_message or "No candidate scored above threshold"])
