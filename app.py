"""YouTube Audio Downloader & Spotify Playlist Importer.

A lightweight CustomTkinter GUI around yt-dlp, aria2c, and FFmpeg with:
- Fast multi-fragment downloads, aria2c external downloader acceleration
- Codec-matching conversion skip and pre-fetched info reuse
- Automatic skip of existing tracks for resume support
- Spotify Playlist Import (Public Embed, Web API PKCE, CSV/Text fallback)
- Multi-threaded parallel queue with per-track and overall progress bars
- Review list with candidate scoring and manual URL override
- End-of-run summary with Retry Failed and Export Failed List
"""

import concurrent.futures
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from spotify_import import (
    SpotifyPKCEAuth,
    SpotifyTrack,
    build_fast_ytdl_opts,
    export_failed_tracks_csv,
    extract_spotify_playlist_id,
    fetch_playlist_embed,
    fetch_playlist_web_api,
    get_aria2c_path,
    match_track_to_youtube,
    parse_exportify_csv,
    parse_plain_text_tracks,
    sanitize_filename,
    should_skip_conversion,
    track_already_exists,
)

FORMATS = {
    "MP3": ("mp3", True),
    "M4A": ("m4a", True),
    "Opus": ("opus", True),
    "FLAC": ("flac", True),
    "WAV": ("wav", True),
    "Best (original, no conversion)": (None, False),
}

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"


def load_config() -> dict:
    defaults = {
        "download_dir": str(Path.home() / "Downloads"),
        "simultaneous_downloads": 3,
        "spotify_client_id": "",
        "spotify_access_token": "",
        "default_format": "MP3",
    }
    if CONFIG_FILE.is_file():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                defaults.update(data)
        except Exception:
            pass
    return defaults


def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def fmt_duration(seconds):
    if not seconds or seconds < 0:
        return "--:--"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")


# ==============================================================================
# MAIN APPLICATION
# ==============================================================================

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Audio Downloader")
        self.geometry("640x600")
        self.minsize(580, 540)

        self.q = queue.Queue()
        self.busy = False
        self.has_ffmpeg = shutil.which("ffmpeg") is not None
        self.aria2c_path = get_aria2c_path()

        self.cfg = load_config()
        self.out_dir = Path(self.cfg.get("download_dir", str(Path.home() / "Downloads")))
        self.cached_info = None  # Reusing already fetched video info (Task A.5)

        self._build_ui()
        self.after(100, self._poll)

    # ---------- Main UI ----------
    def _build_ui(self):
        pad = {"padx": 20}

        # Header with Settings
        head_frame = ctk.CTkFrame(self, fg_color="transparent")
        head_frame.pack(fill="x", pady=(16, 6), **pad)
        ctk.CTkLabel(
            head_frame,
            text="YouTube Audio Downloader",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left")

        ctk.CTkButton(
            head_frame,
            text="⚙ Settings",
            width=85,
            command=self.open_settings,
        ).pack(side="right")

        # URL Input & Buttons Row
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", pady=(6, 10), **pad)

        self.url = ctk.CTkEntry(
            row,
            placeholder_text="Paste YouTube URL or Spotify Playlist link here...",
        )
        self.url.pack(side="left", fill="x", expand=True)
        self.url.bind("<Return>", lambda e: self.on_fetch_clicked())
        self.url.bind("<<Paste>>", lambda e: self.after(50, self.on_fetch_clicked))

        ctk.CTkButton(
            row,
            text="Fetch",
            width=65,
            command=self.on_fetch_clicked,
        ).pack(side="left", padx=(8, 4))

        ctk.CTkButton(
            row,
            text="Import Spotify",
            width=115,
            fg_color="#1DB954",
            hover_color="#1aa34a",
            command=self.open_spotify_import_dialog,
        ).pack(side="left")

        # Video Info Card
        card = ctk.CTkFrame(self)
        card.pack(fill="x", pady=10, **pad)

        self.thumb = ctk.CTkLabel(card, text="", width=160, height=90)
        self.thumb.pack(side="left", padx=10, pady=10)

        info = ctk.CTkFrame(card, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, pady=10)

        self.title_lbl = ctk.CTkLabel(
            info,
            text="No video or playlist loaded",
            wraplength=340,
            justify="left",
            anchor="w",
            font=ctk.CTkFont(weight="bold"),
        )
        self.title_lbl.pack(fill="x")

        self.meta_lbl = ctk.CTkLabel(info, text="", anchor="w", text_color="gray")
        self.meta_lbl.pack(fill="x")

        # Format Picker
        frow = ctk.CTkFrame(self, fg_color="transparent")
        frow.pack(fill="x", pady=(6, 4), **pad)
        ctk.CTkLabel(frow, text="Audio Format").pack(side="left")
        default_fmt = self.cfg.get("default_format", "MP3")
        if default_fmt not in FORMATS:
            default_fmt = "MP3"
        self.fmt = ctk.CTkOptionMenu(frow, values=list(FORMATS), width=240)
        self.fmt.set(default_fmt)
        self.fmt.pack(side="right")

        # Destination Folder
        drow = ctk.CTkFrame(self, fg_color="transparent")
        drow.pack(fill="x", pady=6, **pad)
        ctk.CTkButton(drow, text="Choose folder", width=110, command=self.pick_dir).pack(side="left")
        self.dir_lbl = ctk.CTkLabel(drow, text=str(self.out_dir), anchor="w")
        self.dir_lbl.pack(side="left", fill="x", expand=True, padx=10)

        # Single Progress Bar & Status
        self.bar = ctk.CTkProgressBar(self)
        self.bar.set(0)
        self.bar.pack(fill="x", pady=(10, 4), **pad)

        self.status = ctk.CTkLabel(self, text="Ready", text_color="gray")
        self.status.pack(**pad)

        # Accelerator Badge
        accel_text = []
        if self.aria2c_path:
            accel_text.append("⚡ aria2c active (16 conns)")
        if self.has_ffmpeg:
            accel_text.append("✓ FFmpeg ready")
        else:
            accel_text.append("⚠ FFmpeg not found (Best quality only)")

        self.accel_lbl = ctk.CTkLabel(
            self,
            text="  •  ".join(accel_text),
            font=ctk.CTkFont(size=11),
            text_color="#388e3c" if self.has_ffmpeg else "#d32f2f",
        )
        self.accel_lbl.pack(pady=(2, 6))

        # Main Download Button
        self.btn = ctk.CTkButton(
            self,
            text="Download",
            height=40,
            command=self.download_single,
            state="disabled",
        )
        self.btn.pack(fill="x", pady=(8, 14), **pad)

    # ---------- User Actions ----------
    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.out_dir)
        if d:
            self.out_dir = Path(d)
            self.dir_lbl.configure(text=d)
            self.cfg["download_dir"] = d
            save_config(self.cfg)

    def on_fetch_clicked(self):
        url = self.url.get().strip()
        if not url or self.busy:
            return

        # Check if URL is a Spotify playlist link
        if "spotify.com" in url or "spotify:playlist" in url:
            self.open_spotify_import_dialog(prefill_url=url)
            return

        # Standard YouTube fetch
        self._set_busy(True, "Fetching info from YouTube...")
        threading.Thread(target=self._fetch_worker, args=(url,), daemon=True).start()

    def download_single(self):
        url = self.url.get().strip()
        codec, needs_ff = FORMATS[self.fmt.get()]
        if needs_ff and not self.has_ffmpeg:
            self.status.configure(
                text="Install FFmpeg for conversion, or pick 'Best (original)'.",
                text_color="orange",
            )
            return

        self.bar.set(0)
        self._set_busy(True, "Preparing fast download...")
        threading.Thread(target=self._download_worker, args=(url, codec), daemon=True).start()

    def _set_busy(self, busy, msg=None):
        self.busy = busy
        self.btn.configure(state="disabled" if busy else "normal")
        if msg:
            self.status.configure(text=msg, text_color="gray")

    # ---------- Background Workers ----------
    def _fetch_worker(self, url):
        try:
            with YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True}) as ydl:
                info = ydl.extract_info(url, download=False)
            self.cached_info = info  # Task A.5: Store info to reuse on download

            img = None
            if info.get("thumbnail"):
                try:
                    data = urllib.request.urlopen(info["thumbnail"], timeout=10).read()
                    img = Image.open(io.BytesIO(data)).convert("RGB")
                except Exception:
                    pass
            self.q.put(("info", info, img))
        except DownloadError as e:
            self.q.put(("error", self._clean(e)))
        except Exception as e:
            self.q.put(("error", str(e)))

    def _download_worker(self, url, codec):
        # 1. Skip files that already exist in output folder (Task A.6)
        expected_title = (
            self.cached_info.get("title")
            if self.cached_info and (self.cached_info.get("webpage_url") == url or self.cached_info.get("original_url") == url)
            else "audio"
        )
        clean_name = sanitize_filename(expected_title)
        existing = track_already_exists(self.out_dir, clean_name, codec)
        if existing:
            self.q.put(("done", f"File already exists: {existing.name} (Skipped)"))
            return

        # 2. Skip conversion when chosen format equals source codec (Task A.4)
        source_acodec = None
        if self.cached_info:
            source_acodec = self.cached_info.get("acodec")

        def hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    pct = d["downloaded_bytes"] / total
                    speed = d.get("_speed_str", "").strip()
                    eta = d.get("_eta_str", "").strip()
                    self.q.put(("progress", pct, f"Downloading {pct*100:.0f}%  {speed}  ETA {eta}"))
            elif d["status"] == "finished":
                self.q.put(("progress", 1.0, "Processing audio / metadata..."))

        opts = build_fast_ytdl_opts(
            outtmpl=str(self.out_dir / "%(title)s.%(ext)s"),
            target_codec=codec,
            progress_hook=hook,
            source_acodec=source_acodec,
            embed_metadata=True,
            embed_thumbnail=True,
            has_ffmpeg=self.has_ffmpeg,
        )

        try:
            with YoutubeDL(opts) as ydl:
                # Task A.5: Reuse already fetched video info instead of extracting again
                if self.cached_info and (
                    self.cached_info.get("webpage_url") == url
                    or self.cached_info.get("original_url") == url
                    or self.cached_info.get("id") in url
                ):
                    ydl.process_ie_result(self.cached_info, download=True)
                else:
                    ydl.download([url])
            self.q.put(("done", f"Saved to {self.out_dir}"))
        except DownloadError as e:
            self.q.put(("error", self._clean(e)))
        except Exception as e:
            self.q.put(("error", str(e)))

    @staticmethod
    def _clean(e):
        msg = str(e).replace("ERROR: ", "")
        return msg.splitlines()[-1][:160]

    # ---------- UI Updates from Queue ----------
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "info":
                    _, info, img = msg
                    self.title_lbl.configure(text=info.get("title", "Unknown"))
                    self.meta_lbl.configure(
                        text=f"{info.get('uploader', '')}  •  {fmt_duration(info.get('duration'))}"
                    )
                    if img:
                        img.thumbnail((160, 90))
                        self.thumb_img = ctk.CTkImage(img, size=img.size)
                        self.thumb.configure(image=self.thumb_img)
                    self._set_busy(False, "Ready to download")
                elif kind == "progress":
                    self.bar.set(msg[1])
                    self.status.configure(text=msg[2], text_color="gray")
                elif kind == "done":
                    self.bar.set(1)
                    self._set_busy(False)
                    desc = msg[1] if len(msg) > 1 else f"Done! Saved to {self.out_dir}"
                    self.status.configure(text=f"Done! {desc}", text_color="green")
                elif kind == "error":
                    self._set_busy(False)
                    self.status.configure(text=f"Error: {msg[1]}", text_color="red")

                if not self.busy and self.title_lbl.cget("text") not in (
                    "No video or playlist loaded",
                    "Fetching info...",
                ):
                    self.btn.configure(state="normal")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---------- Dialogs & Windows ----------
    def open_settings(self):
        SettingsDialog(self)

    def open_spotify_import_dialog(self, prefill_url: str = ""):
        SpotifyImportDialog(self, prefill_url=prefill_url)


# ==============================================================================
# SETTINGS DIALOG
# ==============================================================================

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, master: App):
        super().__init__(master)
        self.master_app = master
        self.title("Settings")
        self.geometry("480x420")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 24}
        ctk.CTkLabel(
            self,
            text="Application Settings",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(20, 14), **pad)

        # Simultaneous downloads (Task A.3: 1-5, default 3)
        sim_row = ctk.CTkFrame(self, fg_color="transparent")
        sim_row.pack(fill="x", pady=8, **pad)
        ctk.CTkLabel(sim_row, text="Simultaneous Downloads:").pack(side="left")
        self.sim_opt = ctk.CTkOptionMenu(
            sim_row,
            values=["1", "2", "3", "4", "5"],
            width=80,
        )
        self.sim_opt.set(str(self.master_app.cfg.get("simultaneous_downloads", 3)))
        self.sim_opt.pack(side="right")

        # Spotify Client ID (Task B.1.b)
        ctk.CTkLabel(
            self,
            text="Spotify Web API (Optional for private/large playlists):",
            anchor="w",
            font=ctk.CTkFont(weight="bold"),
        ).pack(fill="x", pady=(14, 4), **pad)

        self.cid_entry = ctk.CTkEntry(
            self,
            placeholder_text="Enter your Spotify Client ID (PKCE)",
        )
        self.cid_entry.pack(fill="x", **pad)
        if self.master_app.cfg.get("spotify_client_id"):
            self.cid_entry.insert(0, self.master_app.cfg["spotify_client_id"])

        token_row = ctk.CTkFrame(self, fg_color="transparent")
        token_row.pack(fill="x", pady=(6, 12), **pad)
        self.pkce_btn = ctk.CTkButton(
            token_row,
            text="Authorize with Spotify (PKCE)",
            fg_color="#1DB954",
            hover_color="#1aa34a",
            command=self.start_pkce_flow,
        )
        self.pkce_btn.pack(side="left", fill="x", expand=True)

        self.token_status = ctk.CTkLabel(
            self,
            text="Public embed method works without Client ID!" if not self.master_app.cfg.get("spotify_access_token") else "✓ Spotify Web API authorized",
            font=ctk.CTkFont(size=12),
            text_color="#1DB954" if self.master_app.cfg.get("spotify_access_token") else "gray",
        )
        self.token_status.pack(**pad)

        # Accelerators Status
        env_frame = ctk.CTkFrame(self)
        env_frame.pack(fill="x", pady=14, **pad)
        ctk.CTkLabel(
            env_frame,
            text=f"Aria2c Acceleration: {'Detected (16x parallel)' if self.master_app.aria2c_path else 'Not found (Using built-in)'}",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 2))
        ctk.CTkLabel(
            env_frame,
            text=f"FFmpeg Status: {'Detected' if self.master_app.has_ffmpeg else 'Not found'}",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(2, 8))

        # Save Button
        ctk.CTkButton(self, text="Save Settings", height=36, command=self.save).pack(
            fill="x", pady=16, **pad
        )

    def start_pkce_flow(self):
        cid = self.cid_entry.get().strip()
        if not cid:
            messagebox.showwarning("Spotify Client ID", "Please enter your Spotify Client ID first.")
            return

        verifier, challenge = SpotifyPKCEAuth.generate_pkce()
        auth_url = SpotifyPKCEAuth.build_authorize_url(cid, challenge)

        self.pkce_btn.configure(state="disabled", text="Waiting for authorization...")

        def run_auth_server():
            auth_code = None

            class CallbackHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    nonlocal auth_code
                    if "/callback" in self.path:
                        query = urllib.parse.urlparse(self.path).query
                        params = urllib.parse.parse_qs(query)
                        if "code" in params:
                            auth_code = params["code"][0]
                            self.send_response(200)
                            self.send_header("Content-Type", "text/html; charset=utf-8")
                            self.end_headers()
                            self.wfile.write(
                                b"<h2>Spotify Authorization Successful!</h2><p>You can close this tab now.</p>"
                            )
                        else:
                            self.send_response(400)
                            self.end_headers()

                def log_message(self, format, *args):
                    pass

            try:
                server = HTTPServer(("127.0.0.1", 8888), CallbackHandler)
                server.timeout = 60
                webbrowser.open(auth_url)
                server.handle_request()
                server.server_close()

                if auth_code:
                    token_data = SpotifyPKCEAuth.exchange_code_for_token(cid, auth_code, verifier)
                    access_token = token_data.get("access_token")
                    if access_token:
                        self.master_app.cfg["spotify_access_token"] = access_token
                        save_config(self.master_app.cfg)
                        self.after(0, lambda: self.token_status.configure(
                            text="✓ Spotify Web API authorized", text_color="#1DB954"
                        ))
                        self.after(0, lambda: messagebox.showinfo("Success", "Spotify connected successfully!"))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Auth Error", f"PKCE auth failed: {e}"))
            finally:
                self.after(0, lambda: self.pkce_btn.configure(state="normal", text="Authorize with Spotify (PKCE)"))

        threading.Thread(target=run_auth_server, daemon=True).start()

    def save(self):
        self.master_app.cfg["simultaneous_downloads"] = int(self.sim_opt.get())
        self.master_app.cfg["spotify_client_id"] = self.cid_entry.get().strip()
        save_config(self.master_app.cfg)
        self.destroy()


# ==============================================================================
# SPOTIFY IMPORT & QUEUE DIALOG (TASK B)
# ==============================================================================

class SpotifyImportDialog(ctk.CTkToplevel):
    def __init__(self, master: App, prefill_url: str = ""):
        super().__init__(master)
        self.master_app = master
        self.title("Import Spotify Playlist")
        self.geometry("740x650")
        self.minsize(680, 560)
        self.transient(master)

        self.playlist_name = "Spotify Playlist"
        self.cover_url = ""
        self.tracks: list[SpotifyTrack] = []
        self.active_workers = 0
        self.cancelled = False

        self._build_ui(prefill_url)
        if prefill_url:
            self.after(150, self.start_fetch_and_match)

    def _build_ui(self, prefill_url: str):
        pad = {"padx": 20}

        # Header
        top_frame = ctk.CTkFrame(self, fg_color="transparent")
        top_frame.pack(fill="x", pady=(16, 10), **pad)
        ctk.CTkLabel(
            top_frame,
            text="Spotify Playlist Importer",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(side="left")

        # URL Input Row
        url_frame = ctk.CTkFrame(self, fg_color="transparent")
        url_frame.pack(fill="x", pady=6, **pad)

        self.url_entry = ctk.CTkEntry(
            url_frame,
            placeholder_text="Paste open.spotify.com/playlist/... link",
        )
        self.url_entry.pack(side="left", fill="x", expand=True)
        if prefill_url:
            self.url_entry.insert(0, prefill_url)

        self.fetch_btn = ctk.CTkButton(
            url_frame,
            text="Fetch & Match",
            width=110,
            command=self.start_fetch_and_match,
        )
        self.fetch_btn.pack(side="left", padx=(8, 0))

        # Manual Fallback Row (Task B.1.c)
        fallback_frame = ctk.CTkFrame(self, fg_color="transparent")
        fallback_frame.pack(fill="x", pady=(2, 10), **pad)
        ctk.CTkButton(
            fallback_frame,
            text="Import from CSV / Plain Text",
            fg_color="#37474f",
            hover_color="#455a64",
            command=self.open_manual_import_modal,
        ).pack(side="left")

        # Progress / Status Header
        self.status_lbl = ctk.CTkLabel(
            self,
            text="Enter a playlist URL or click Import from CSV / Text",
            text_color="gray",
            anchor="w",
        )
        self.status_lbl.pack(fill="x", **pad)

        # Overall Progress Bar
        self.overall_bar = ctk.CTkProgressBar(self)
        self.overall_bar.set(0)
        self.overall_bar.pack(fill="x", pady=(6, 12), **pad)

        # Playlist Info Container
        self.info_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.info_frame.pack(fill="x", pady=(0, 6), **pad)
        self.pl_title_lbl = ctk.CTkLabel(
            self.info_frame,
            text="",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        )
        self.pl_title_lbl.pack(side="left", fill="x", expand=True)

        # Review & Queue Scrollable Container
        self.scroll_list = ctk.CTkScrollableFrame(self, height=280)
        self.scroll_list.pack(fill="both", expand=True, pady=6, **pad)

        # Bottom Action Bar
        self.action_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.action_frame.pack(fill="x", pady=14, **pad)

        self.select_all_btn = ctk.CTkButton(
            self.action_frame,
            text="Select All",
            width=90,
            command=self.select_all_tracks,
            state="disabled",
        )
        self.select_all_btn.pack(side="left", padx=(0, 6))

        self.deselect_all_btn = ctk.CTkButton(
            self.action_frame,
            text="Deselect All",
            width=95,
            command=self.deselect_all_tracks,
            state="disabled",
        )
        self.deselect_all_btn.pack(side="left")

        self.download_btn = ctk.CTkButton(
            self.action_frame,
            text="Download Selected",
            height=36,
            fg_color="#1DB954",
            hover_color="#1aa34a",
            state="disabled",
            command=self.start_queue_downloads,
        )
        self.download_btn.pack(side="right")

        self.retry_btn = ctk.CTkButton(
            self.action_frame,
            text="Retry Failed",
            width=100,
            command=self.retry_failed,
        )
        # Hidden until completion

        self.export_failed_btn = ctk.CTkButton(
            self.action_frame,
            text="Export Failed",
            width=105,
            command=self.export_failed,
        )
        # Hidden until completion

        self.track_widgets: dict[str, dict] = {}

    # ---------- Fetching & Fallbacks (Task B.1) ----------
    def start_fetch_and_match(self):
        url = self.url_entry.get().strip()
        pid = extract_spotify_playlist_id(url)
        if not pid:
            messagebox.showerror("Invalid URL", "Please enter a valid Spotify playlist URL or ID.")
            return

        self.fetch_btn.configure(state="disabled")
        self.status_lbl.configure(text="Fetching playlist tracklist...", text_color="gray")
        self.overall_bar.set(0)

        threading.Thread(target=self._fetch_tracks_worker, args=(pid,), daemon=True).start()

    def _fetch_tracks_worker(self, playlist_id: str):
        playlist_name = "Spotify Playlist"
        cover_url = ""
        tracks = []
        err_msg = ""

        # Method A: Public embed page (Task B.1.a)
        try:
            self.after(0, lambda: self.status_lbl.configure(text="Trying Method A: Public embed page..."))
            playlist_name, cover_url, tracks = fetch_playlist_embed(playlist_id)
        except Exception as eA:
            err_msg = str(eA)
            # Method B: Spotify Web API (Task B.1.b)
            token = self.master_app.cfg.get("spotify_access_token")
            if token:
                try:
                    self.after(0, lambda: self.status_lbl.configure(text="Trying Method B: Spotify Web API..."))
                    playlist_name, cover_url, tracks = fetch_playlist_web_api(playlist_id, token)
                except Exception as eB:
                    err_msg = f"Embed failed ({eA}); Web API failed ({eB})"
            else:
                err_msg = f"Embed failed ({eA}) - No Spotify Web API token in Settings"

        if not tracks:
            # Method C fallback message (Task B.1.c)
            self.after(0, lambda: self._on_fetch_failed(err_msg))
            return

        self.playlist_name = playlist_name
        self.cover_url = cover_url
        self.tracks = tracks

        self.after(0, lambda: self.status_lbl.configure(
            text=f"Loaded {len(tracks)} tracks. Searching YouTube & scoring matches..."
        ))
        self._start_matching_candidates()

    def _on_fetch_failed(self, error: str):
        self.fetch_btn.configure(state="normal")
        self.status_lbl.configure(
            text=f"Fetch failed: {error[:100]}. Please use 'Import from CSV / Text'.",
            text_color="orange",
        )
        messagebox.showinfo(
            "Spotify Import Fallback",
            f"Could not load playlist via embed or Web API:\n{error}\n\n"
            "You can use the 'Import from CSV / Plain Text' button to load the tracks!",
        )

    # ---------- YouTube Candidate Matching (Task B.2) ----------
    def _start_matching_candidates(self):
        threading.Thread(target=self._matching_worker, daemon=True).start()

    def _matching_worker(self):
        total = len(self.tracks)
        matched_count = 0

        with YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True}) as ydl:
            for idx, track in enumerate(self.tracks, start=1):
                best_cand, score = match_track_to_youtube(
                    track, ydl, min_score_threshold=60.0, delay_between_searches=0.4
                )
                if best_cand:
                    track.matched_id = best_cand.get("id", "")
                    track.matched_title = best_cand.get("title", "")
                    track.matched_duration = best_cand.get("duration", 0)
                    track.matched_url = best_cand.get("webpage_url") or f"https://www.youtube.com/watch?v={track.matched_id}"
                    track.matched_channel = best_cand.get("uploader") or best_cand.get("channel") or ""
                    track.score = score
                    track.status = "Pending"
                    track.selected = True
                    matched_count += 1
                else:
                    track.status = "No match"
                    track.selected = False

                pct = idx / total
                self.after(
                    0,
                    lambda i=idx, t=total, title=track.title, p=pct: self._update_match_progress(
                        i, t, title, p
                    ),
                )

        self.after(0, self._render_review_list)

    def _update_match_progress(self, idx, total, title, pct):
        self.overall_bar.set(pct)
        self.status_lbl.configure(text=f"Matching {idx}/{total}: {title[:40]}...")

    # ---------- Review Screen (Task B.3) ----------
    def _render_review_list(self):
        self.fetch_btn.configure(state="normal")
        self.select_all_btn.configure(state="normal")
        self.deselect_all_btn.configure(state="normal")

        # Clear scroll area
        for w in self.scroll_list.winfo_children():
            w.destroy()
        self.track_widgets.clear()

        matched = sum(1 for t in self.tracks if t.status != "No match")
        self.pl_title_lbl.configure(text=f"{self.playlist_name} ({len(self.tracks)} tracks)")
        self.status_lbl.configure(
            text=f"Review matches: {matched}/{len(self.tracks)} matched. Untick or edit any match before downloading.",
            text_color="gray",
        )
        self.overall_bar.set(0)

        for track in self.tracks:
            row = ctk.CTkFrame(self.scroll_list)
            row.pack(fill="x", pady=4, padx=4)

            # Checkbox
            var = ctk.BooleanVar(value=track.selected)

            def make_toggle(t, v):
                def toggle():
                    t.selected = v.get()
                    self._update_download_btn_text()
                return toggle

            cb = ctk.CTkCheckBox(
                row,
                text="",
                width=24,
                variable=var,
                command=make_toggle(track, var),
            )
            cb.pack(side="left", padx=(8, 4))

            # Info box
            info_frame = ctk.CTkFrame(row, fg_color="transparent")
            info_frame.pack(side="left", fill="x", expand=True, padx=4, pady=4)

            sp_text = f"{track.artists} - {track.title}  ({fmt_duration(track.duration_sec)})"
            sp_lbl = ctk.CTkLabel(info_frame, text=sp_text, anchor="w", font=ctk.CTkFont(weight="bold"))
            sp_lbl.pack(fill="x")

            yt_lbl = ctk.CTkLabel(info_frame, text="", anchor="w", font=ctk.CTkFont(size=12))
            yt_lbl.pack(fill="x")

            # Progress bar for queue phase
            bar = ctk.CTkProgressBar(info_frame, height=6)
            bar.set(0)

            # Status pill
            status_lbl = ctk.CTkLabel(row, text="", width=90, font=ctk.CTkFont(size=11))
            status_lbl.pack(side="right", padx=(4, 8))

            # Edit URL button
            edit_btn = ctk.CTkButton(
                row,
                text="Edit",
                width=50,
                command=lambda t=track, yl=yt_lbl, sl=status_lbl, v=var: self.edit_track_url(
                    t, yl, sl, v
                ),
            )
            edit_btn.pack(side="right", padx=4)

            self.track_widgets[track.id] = {
                "var": var,
                "yt_lbl": yt_lbl,
                "status_lbl": status_lbl,
                "bar": bar,
                "edit_btn": edit_btn,
            }

            self._update_row_labels(track)

        self._update_download_btn_text()

    def _update_row_labels(self, track: SpotifyTrack):
        w = self.track_widgets.get(track.id)
        if not w:
            return

        if track.matched_url:
            w["yt_lbl"].configure(
                text=f"Matched: {track.matched_title}  ({fmt_duration(track.matched_duration)})",
                text_color="gray",
            )
            w["status_lbl"].configure(text=f"Match ({int(track.score)})", text_color="#1DB954")
        else:
            w["yt_lbl"].configure(text="No match found on YouTube", text_color="#e53935")
            w["status_lbl"].configure(text="No match", text_color="#e53935")

    def edit_track_url(self, track: SpotifyTrack, yt_lbl, status_lbl, var):
        dialog = ctk.CTkInputDialog(
            text=f"Enter YouTube URL or Video ID for:\n{track.artists} - {track.title}",
            title="Edit Matched YouTube URL",
        )
        new_val = dialog.get_input()
        if new_val:
            new_val = new_val.strip()
            track.matched_url = new_val
            track.matched_title = "Custom YouTube Link"
            track.status = "Pending"
            track.score = 100.0
            track.selected = True
            var.set(True)
            self._update_row_labels(track)
            self._update_download_btn_text()

    def select_all_tracks(self):
        for t in self.tracks:
            if t.matched_url:
                t.selected = True
                if t.id in self.track_widgets:
                    self.track_widgets[t.id]["var"].set(True)
        self._update_download_btn_text()

    def deselect_all_tracks(self):
        for t in self.tracks:
            t.selected = False
            if t.id in self.track_widgets:
                self.track_widgets[t.id]["var"].set(False)
        self._update_download_btn_text()

    def _update_download_btn_text(self):
        count = sum(1 for t in self.tracks if t.selected and t.matched_url)
        self.download_btn.configure(
            text=f"Download Selected ({count})",
            state="normal" if count > 0 else "disabled",
        )

    # ---------- Parallel Worker Pool Queue (Task A.3 & Task B.4) ----------
    def start_queue_downloads(self):
        selected_tracks = [t for t in self.tracks if t.selected and t.matched_url]
        if not selected_tracks:
            return

        codec, needs_ff = FORMATS[self.master_app.fmt.get()]
        if needs_ff and not self.master_app.has_ffmpeg:
            messagebox.showwarning(
                "FFmpeg Missing",
                "FFmpeg is required for conversion. Please pick 'Best (original)' or install FFmpeg.",
            )
            return

        self.cancelled = False
        self.download_btn.configure(state="disabled")
        self.select_all_btn.configure(state="disabled")
        self.deselect_all_btn.configure(state="disabled")
        self.fetch_btn.configure(state="disabled")

        # Destination folder: out_dir / <Playlist Name>
        playlist_folder = self.master_app.out_dir / sanitize_filename(self.playlist_name)
        playlist_folder.mkdir(parents=True, exist_ok=True)

        # Show miniature progress bars in rows
        for t in selected_tracks:
            w = self.track_widgets.get(t.id)
            if w:
                w["bar"].pack(fill="x", pady=(2, 0))
                w["bar"].set(0)
                w["status_lbl"].configure(text="Queued", text_color="gray")
                w["edit_btn"].configure(state="disabled")

        sim_downloads = self.master_app.cfg.get("simultaneous_downloads", 3)
        self.status_lbl.configure(
            text=f"Starting downloads ({sim_downloads} concurrent workers)..."
        )

        threading.Thread(
            target=self._queue_runner,
            args=(selected_tracks, playlist_folder, codec, sim_downloads),
            daemon=True,
        ).start()

    def _queue_runner(
        self,
        tracks: list[SpotifyTrack],
        folder: Path,
        codec: str | None,
        max_workers: int,
    ):
        total = len(tracks)
        completed_count = 0
        skipped_count = 0
        failed_count = 0
        lock = threading.Lock()

        def update_overall():
            nonlocal completed_count, skipped_count, failed_count
            done_so_far = completed_count + skipped_count + failed_count
            pct = done_so_far / total
            self.after(
                0,
                lambda: self._set_queue_progress(
                    done_so_far, total, skipped_count, failed_count, pct
                ),
            )

        def download_track(track: SpotifyTrack):
            nonlocal completed_count, skipped_count, failed_count

            # Task A.6: Skip files that already exist in destination
            existing = track_already_exists(folder, track.safe_filename_base, codec)
            if existing:
                with lock:
                    skipped_count += 1
                track.status = "Skipped"
                self.after(0, lambda: self._set_track_status(track.id, 1.0, "Skipped", "#388e3c"))
                update_overall()
                return

            def track_hook(d):
                if d["status"] == "downloading":
                    tot = d.get("total_bytes") or d.get("total_bytes_estimate")
                    if tot:
                        pct = d["downloaded_bytes"] / tot
                        spd = d.get("_speed_str", "").strip()
                        self.after(
                            0,
                            lambda p=pct, s=spd: self._set_track_status(
                                track.id, p, f"DL {p*100:.0f}%", "gray"
                            ),
                        )
                elif d["status"] == "finished":
                    self.after(
                        0,
                        lambda: self._set_track_status(track.id, 1.0, "Converting...", "gray"),
                    )

            # Task A & B: Fast yt-dlp options with metadata & cover embedding
            outtmpl = str(folder / f"{track.safe_filename_base}.%(ext)s")
            track_meta = {
                "title": track.title,
                "artist": track.artists,
                "album": track.album or self.playlist_name,
                "track_number": track.track_number,
            }

            opts = build_fast_ytdl_opts(
                outtmpl=outtmpl,
                target_codec=codec,
                progress_hook=track_hook,
                embed_metadata=True,
                embed_thumbnail=True,
                no_overwrites=True,
                track_metadata=track_meta,
                has_ffmpeg=self.master_app.has_ffmpeg,
            )

            try:
                with YoutubeDL(opts) as ydl:
                    ydl.download([track.matched_url])
                with lock:
                    completed_count += 1
                track.status = "Done"
                self.after(0, lambda: self._set_track_status(track.id, 1.0, "Done", "#1DB954"))
            except Exception as e:
                with lock:
                    failed_count += 1
                track.status = "Failed"
                track.error_message = str(e)
                self.after(0, lambda: self._set_track_status(track.id, 0.0, "Failed", "#e53935"))

            update_overall()

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(download_track, t) for t in tracks]
            concurrent.futures.wait(futures)

        self.after(
            0,
            lambda: self._on_queue_finished(
                completed_count, skipped_count, failed_count, len(self.tracks) - total
            ),
        )

    def _set_track_status(self, track_id: str, pct: float, text: str, color: str):
        w = self.track_widgets.get(track_id)
        if w:
            w["bar"].set(pct)
            w["status_lbl"].configure(text=text, text_color=color)

    def _set_queue_progress(self, done, total, skipped, failed, pct):
        self.overall_bar.set(pct)
        self.status_lbl.configure(
            text=f"Queue Progress: {done} / {total} done ({skipped} skipped, {failed} failed)",
            text_color="gray",
        )

    # ---------- Summary & Retry / Export (Task B.5) ----------
    def _on_queue_finished(self, done, skipped, failed, no_match):
        self.overall_bar.set(1.0)
        summary = f"Finished! {done} downloaded, {skipped} skipped (already exists), {failed} failed, {no_match} no match."
        self.status_lbl.configure(text=summary, text_color="#1DB954" if failed == 0 else "orange")

        self.download_btn.configure(state="normal")
        self.fetch_btn.configure(state="normal")
        self.select_all_btn.configure(state="normal")
        self.deselect_all_btn.configure(state="normal")

        # Show Retry & Export buttons
        if failed > 0 or no_match > 0:
            self.retry_btn.pack(side="right", padx=(4, 8))
            self.export_failed_btn.pack(side="right", padx=4)

        messagebox.showinfo(
            "Download Summary",
            f"Playlist Download Complete!\n\n"
            f"• Downloaded: {done}\n"
            f"• Skipped (Already Exists): {skipped}\n"
            f"• Failed: {failed}\n"
            f"• No Match: {no_match}\n\n"
            f"Saved to: {self.master_app.out_dir / sanitize_filename(self.playlist_name)}",
        )

    def retry_failed(self):
        # Reset failed tracks to selected
        for t in self.tracks:
            if t.status in ("Failed", "No match"):
                t.selected = True
                if t.id in self.track_widgets:
                    self.track_widgets[t.id]["var"].set(True)
                    self.track_widgets[t.id]["status_lbl"].configure(
                        text="Pending", text_color="gray"
                    )
        self.start_queue_downloads()

    def export_failed(self):
        f = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"{sanitize_filename(self.playlist_name)}_failed_tracks.csv",
        )
        if f:
            export_failed_tracks_csv(self.tracks, Path(f))
            messagebox.showinfo("Export Successful", f"Failed tracks exported to {f}")

    # ---------- Manual Import Fallback Modal (Task B.1.c) ----------
    def open_manual_import_modal(self):
        ManualImportModal(self)


# ==============================================================================
# MANUAL IMPORT MODAL (CSV / TEXT PASTE)
# ==============================================================================

class ManualImportModal(ctk.CTkToplevel):
    def __init__(self, parent_dialog: SpotifyImportDialog):
        super().__init__(parent_dialog)
        self.parent_dialog = parent_dialog
        self.title("Manual Import (CSV / Plain Text)")
        self.geometry("540x440")
        self.transient(parent_dialog)
        self.grab_set()

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 20}
        ctk.CTkLabel(
            self,
            text="Import from Exportify CSV or Text",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(pady=(16, 8), **pad)

        ctk.CTkLabel(
            self,
            text="Paste 'Artist - Title' lines or load an Exportify CSV file:",
            anchor="w",
            text_color="gray",
        ).pack(fill="x", **pad)

        self.text_area = ctk.CTkTextbox(self, height=200)
        self.text_area.pack(fill="both", expand=True, pady=10, **pad)

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, 16), **pad)

        ctk.CTkButton(
            btn_row,
            text="Load CSV File",
            width=120,
            command=self.load_csv_file,
        ).pack(side="left")

        ctk.CTkButton(
            btn_row,
            text="Parse & Load Tracks",
            width=150,
            fg_color="#1DB954",
            hover_color="#1aa34a",
            command=self.parse_and_submit,
        ).pack(side="right")

    def load_csv_file(self):
        f = filedialog.askopenfilename(
            filetypes=[("CSV files", "*.csv"), ("Text files", "*.txt"), ("All files", "*.*")]
        )
        if f:
            content = Path(f).read_text(encoding="utf-8", errors="replace")
            self.text_area.delete("1.0", "end")
            self.text_area.insert("1.0", content)

    def parse_and_submit(self):
        raw = self.text_area.get("1.0", "end").strip()
        if not raw:
            return

        tracks = parse_exportify_csv(raw)
        if not tracks:
            tracks = parse_plain_text_tracks(raw)

        if not tracks:
            messagebox.showerror(
                "Parse Error",
                "Could not extract tracks. Please paste 'Artist - Title' per line or a valid Exportify CSV.",
            )
            return

        self.parent_dialog.playlist_name = "Imported Playlist"
        self.parent_dialog.tracks = tracks
        self.parent_dialog.status_lbl.configure(
            text=f"Loaded {len(tracks)} tracks from manual import. Searching YouTube..."
        )
        self.destroy()
        self.parent_dialog._start_matching_candidates()


if __name__ == "__main__":
    app = App()
    app.mainloop()

