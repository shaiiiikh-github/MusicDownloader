"""Unit tests for spotify_import and app modules."""

import csv
import json
from pathlib import Path
from spotify_import import (
    DownloadHistoryManager,
    SpotifyPKCEAuth,
    SpotifyTrack,
    build_fast_ytdl_opts,
    export_failed_tracks_csv,
    extract_spotify_playlist_id,
    get_download_history,
    match_track_to_youtube,
    parse_embed_json,
    parse_exportify_csv,
    parse_plain_text_tracks,
    sanitize_filename,
    score_match,
    should_skip_conversion,
    track_already_exists,
)
from app import FORMATS, load_config, save_config


def test_sanitize_filename():
    assert sanitize_filename("AC/DC: Back in Black? *special*") == "ACDC Back in Black special"
    assert sanitize_filename("Artist / Title <3>") == "Artist Title 3"
    assert sanitize_filename("") == "Unknown"
    assert sanitize_filename("a" * 200) == "a" * 150


def test_extract_spotify_playlist_id():
    pid = "37i9dQZF1DXcBWIGoYBM5M"
    assert extract_spotify_playlist_id(f"https://open.spotify.com/playlist/{pid}") == pid
    assert extract_spotify_playlist_id(f"https://open.spotify.com/playlist/{pid}?si=abcd1234") == pid
    assert extract_spotify_playlist_id(f"spotify:playlist:{pid}") == pid
    assert extract_spotify_playlist_id(f"https://open.spotify.com/embed/playlist/{pid}") == pid
    assert extract_spotify_playlist_id(pid) == pid
    assert extract_spotify_playlist_id("https://youtube.com/watch?v=123") is None


def test_should_skip_conversion():
    # Matching codecs (skip re-encoding)
    assert should_skip_conversion("opus", "opus") is True
    assert should_skip_conversion("opus", "Opus") is True
    assert should_skip_conversion("aac", "m4a") is True
    assert should_skip_conversion("mp4a.40.2", "m4a") is True
    assert should_skip_conversion("mp3", "mp3") is True
    assert should_skip_conversion("flac", "flac") is True
    assert should_skip_conversion("opus", None) is True
    assert should_skip_conversion("opus", "best") is True

    # Differing codecs (needs conversion)
    assert should_skip_conversion("opus", "mp3") is False
    assert should_skip_conversion("opus", "flac") is False
    assert should_skip_conversion("aac", "mp3") is False


def test_score_match_topic_and_duration():
    track = SpotifyTrack(
        id="t1",
        title="Blinding Lights",
        artists="The Weeknd",
        duration_ms=200000,  # 200 sec
    )

    # Topic channel + exact duration
    cand_topic = {
        "title": "Blinding Lights",
        "uploader": "The Weeknd - Topic",
        "channel": "The Weeknd - Topic",
        "duration": 200,
        "description": "Provided to YouTube by Universal Music Group",
    }
    score_topic = score_match(track, cand_topic)
    assert score_topic >= 180

    # Live Acoustic Cover penalty
    cand_cover = {
        "title": "Blinding Lights (Live Acoustic Cover)",
        "uploader": "Random Singer",
        "channel": "Random Singer",
        "duration": 200,
        "description": "Cover version",
    }
    score_cover = score_match(track, cand_cover)
    assert score_cover < 80
    assert score_cover < score_topic


def test_score_match_allowed_keyword_when_in_track_title():
    track_remix = SpotifyTrack(
        id="t2",
        title="Levitating (The Blessed Madonna Remix)",
        artists="Dua Lipa",
        duration_ms=250000,
    )
    cand_remix = {
        "title": "Dua Lipa - Levitating (The Blessed Madonna Remix)",
        "uploader": "Dua Lipa - Topic",
        "channel": "Dua Lipa",
        "duration": 250,
        "description": "Provided to YouTube by Warner Records",
    }
    score = score_match(track_remix, cand_remix)
    assert score >= 170


def test_score_match_duration_penalty():
    track = SpotifyTrack(
        id="t3",
        title="Song A",
        artists="Artist A",
        duration_ms=180000,  # 3 min
    )
    cand_long = {
        "title": "Song A - 10 Hour Loop",
        "uploader": "Random",
        "channel": "Random",
        "duration": 600,
        "description": "",
    }
    score_long = score_match(track, cand_long)
    assert score_long < 50


def test_parse_embed_json():
    sample_payload = {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": {
                            "title": "Awesome Hits",
                            "visualIdentity": {
                                "image": [
                                    {"url": "https://example.com/cover_small.jpg", "maxWidth": 64},
                                    {"url": "https://example.com/cover_large.jpg", "maxWidth": 640},
                                ]
                            },
                            "trackList": [
                                {
                                    "title": "Song One",
                                    "subtitle": "Artist One",
                                    "duration": 180000,
                                    "uri": "spotify:track:track1",
                                },
                                {
                                    "title": "Song Two",
                                    "subtitle": "Artist Two, Featured Artist",
                                    "duration": 210000,
                                    "uri": "spotify:track:track2",
                                }
                            ]
                        }
                    }
                }
            }
        }
    }
    title, cover, tracks = parse_embed_json(sample_payload)
    assert title == "Awesome Hits"
    assert cover == "https://example.com/cover_large.jpg"
    assert len(tracks) == 2
    assert tracks[0].title == "Song One"
    assert tracks[0].artists == "Artist One"
    assert tracks[0].duration_ms == 180000
    assert tracks[1].title == "Song Two"


def test_parse_exportify_csv():
    csv_data = """Spotify ID,Artist Name(s),Track Name,Album Name,Track Duration (ms)
111,Queen,Bohemian Rhapsody,A Night at the Opera,354000
222,Eagles,Hotel California,Hotel California,391000
"""
    tracks = parse_exportify_csv(csv_data)
    assert len(tracks) == 2
    assert tracks[0].title == "Bohemian Rhapsody"
    assert tracks[0].artists == "Queen"
    assert tracks[0].album == "A Night at the Opera"
    assert tracks[0].duration_ms == 354000
    assert tracks[1].title == "Hotel California"


def test_parse_plain_text():
    text_data = """
# Favorites
Queen - Bohemian Rhapsody
The Beatles - Hey Jude
Lone Track Without Dash
"""
    tracks = parse_plain_text_tracks(text_data)
    assert len(tracks) == 3
    assert tracks[0].artists == "Queen"
    assert tracks[0].title == "Bohemian Rhapsody"
    assert tracks[1].artists == "The Beatles"
    assert tracks[1].title == "Hey Jude"
    assert tracks[2].artists == "Unknown Artist"
    assert tracks[2].title == "Lone Track Without Dash"


def test_build_fast_ytdl_opts_speed_and_metadata():
    track_meta = {
        "title": "Bohemian Rhapsody",
        "artist": "Queen",
        "album": "A Night at the Opera",
        "track_number": 1,
    }
    opts = build_fast_ytdl_opts(
        "out.mp3",
        target_codec="mp3",
        track_metadata=track_meta,
        has_ffmpeg=True,
    )
    assert opts["concurrent_fragment_downloads"] == 8
    assert opts["retries"] == 10
    assert opts["fragment_retries"] == 10
    assert opts["http_chunk_size"] == 10 * 1024 * 1024
    assert opts["format"] == "bestaudio/best"

    pp_keys = [p["key"] for p in opts.get("postprocessors", [])]
    assert "FFmpegExtractAudio" in pp_keys
    assert "FFmpegMetadata" in pp_keys

    meta_args = opts.get("postprocessor_args", {}).get("FFmpegMetadata", [])
    assert "-metadata" in meta_args
    assert "title=Bohemian Rhapsody" in meta_args
    assert "artist=Queen" in meta_args
    assert "album=A Night at the Opera" in meta_args


def test_track_already_exists(tmp_path: Path):
    out_dir = tmp_path / "downloads"
    out_dir.mkdir()
    sample_file = out_dir / "Queen - Bohemian Rhapsody.mp3"
    sample_file.write_bytes(b"0" * 2048)

    found = track_already_exists(out_dir, "Queen - Bohemian Rhapsody", "mp3")
    assert found is not None
    assert found.name == "Queen - Bohemian Rhapsody.mp3"

    not_found = track_already_exists(out_dir, "Nonexistent - Song", "mp3")
    assert not_found is None


def test_export_failed_tracks_csv(tmp_path: Path):
    tracks = [
        SpotifyTrack(id="1", title="Track One", artists="Artist A", status="Done"),
        SpotifyTrack(id="2", title="Track Two", artists="Artist B", status="Failed", error_message="Network error"),
        SpotifyTrack(id="3", title="Track Three", artists="Artist C", status="No match"),
    ]
    out_file = tmp_path / "failed.csv"
    export_failed_tracks_csv(tracks, out_file)
    assert out_file.is_file()

    with open(out_file, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert len(rows) == 3  # Header + 2 failed tracks
    assert rows[1][1] == "Artist B"
    assert rows[1][4] == "Failed"
    assert rows[2][1] == "Artist C"
    assert rows[2][4] == "No match"


def test_spotify_pkce_auth_helpers():
    verifier, challenge = SpotifyPKCEAuth.generate_pkce()
    assert len(verifier) >= 43
    assert len(challenge) > 0
    url = SpotifyPKCEAuth.build_authorize_url("test_client_id", challenge)
    assert "test_client_id" in url
    assert challenge in url
    assert "code_challenge_method=S256" in url


def test_match_track_mock_ydl():
    class MockYDL:
        def extract_info(self, query, download=False):
            return {
                "entries": [
                    {
                        "id": "vid1",
                        "title": "The Weeknd - Blinding Lights (Official Audio)",
                        "uploader": "The Weeknd - Topic",
                        "channel": "The Weeknd - Topic",
                        "duration": 200,
                        "description": "Provided to YouTube by Universal",
                        "webpage_url": "https://www.youtube.com/watch?v=vid1",
                    },
                    {
                        "id": "vid2",
                        "title": "Blinding Lights - 1 Hour Loop Live Cover",
                        "uploader": "Random",
                        "duration": 3600,
                        "description": "",
                        "webpage_url": "https://www.youtube.com/watch?v=vid2",
                    }
                ]
            }

    track = SpotifyTrack(id="t1", title="Blinding Lights", artists="The Weeknd", duration_ms=200000)
    cand, score = match_track_to_youtube(track, MockYDL(), min_score_threshold=60.0, delay_between_searches=0)
    assert cand is not None
    assert cand["id"] == "vid1"
    assert score > 100


def test_download_history_manager(tmp_path: Path):
    hist_file = tmp_path / "history.json"
    mgr = DownloadHistoryManager(hist_file)

    # 1. Test key normalization
    assert mgr.normalize_key("Queen", "Bohemian Rhapsody (Remastered 2011)") == "queen:bohemianrhapsody"
    assert mgr.normalize_key("The Weeknd", "Blinding Lights - Official Audio") == "theweeknd:blindinglights"
    assert mgr.normalize_key("", "Lone Song (HD)") == "lonesong"

    # 2. Record by Spotify ID and check
    mgr.record_download(
        title="Bohemian Rhapsody",
        artist="Queen",
        spotify_id="spotify_123",
        youtube_id="yt_123",
        file_path="C:/Downloads/Queen - Bohemian Rhapsody.mp3",
    )
    assert mgr.count() >= 1

    # Check by Spotify ID
    is_dl, rec = mgr.is_downloaded(spotify_id="spotify_123")
    assert is_dl is True
    assert rec["title"] == "Bohemian Rhapsody"

    # Check by YouTube ID
    is_dl, rec = mgr.is_downloaded(youtube_id="yt_123")
    assert is_dl is True

    # Check by normalized title & artist variant
    is_dl, rec = mgr.is_downloaded(title="Bohemian Rhapsody (2011 Remaster)", artist="Queen")
    assert is_dl is True

    # Check by "Artist - Title" combined string
    is_dl, rec = mgr.is_downloaded(title="Queen - Bohemian Rhapsody")
    assert is_dl is True

    # Non-existent track
    is_dl, rec = mgr.is_downloaded(title="Nonexistent Track", artist="Nobody")
    assert is_dl is False

    # 3. Test scanning and indexing a folder
    downloads_dir = tmp_path / "music_folder"
    downloads_dir.mkdir()
    song_file = downloads_dir / "Eagles - Hotel California.mp3"
    song_file.write_bytes(b"0" * 2048)

    added = mgr.scan_and_index_folder(downloads_dir)
    assert added == 1
    is_dl, _ = mgr.is_downloaded(title="Hotel California", artist="Eagles")
    assert is_dl is True

    # 4. Test clear
    mgr.clear()
    assert mgr.count() == 0
    is_dl, _ = mgr.is_downloaded(spotify_id="spotify_123")
    assert is_dl is False
