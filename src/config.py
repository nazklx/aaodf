from __future__ import annotations
import json
import os
from pathlib import Path

# AAODF_CONFIG lets the web UI run a job with per-job overrides
CONFIG_FILE = Path(os.environ.get("AAODF_CONFIG") or "config.json")

DEFAULT_CONFIG = {
    "announce_url": "#",
    "tmdb_api_key": "#",
    "tracker_api_token": "#",
    "upload_endpoint": "#",
    "confirm_before_upload": True,
    "anonymous_upload": False,
    "personal_release": False,
    "stream_optimized": False,
    # Promotion flags sent with every upload (the tracker ignores them unless your account may set them).
    "free": 0,              # freeleech percentage: 0 = off, 1-100 = that % free
    "free_days": 0,         # days the freeleech lasts, 0 = never expires
    "doubleup": False,      # double upload
    "doubleup_days": 0,     # days double upload lasts, 0 = never expires
    "featured": False,      # featured / sticky
    "group_separator": "-",
    "run_as_user": "",
    "tracker_search_endpoint": "",
    # Tracker search requests per minute (duplicate scans). Deliberately well under the
    # ~60/min many UNIT3D trackers allow per API token, since pagination also counts.
    "tracker_search_rate_limit": 30,
    "download_client": None,

    # ---- Music (MusicBrainz) and books / audiobooks (Open Library) ----
    # "auto" detects video / music / audiobook / book from the files. Force one with
    # video, music, audiobook or book (also settable per job in the web UI or with --type).
    "media_type": "auto",
    "musicbrainz_enabled": True,
    "openlibrary_enabled": True,
    # Without a match there is no source link to hand to the tracker, so by default the upload is
    # skipped. Set to false to upload anyway (named after the folder, description without a link).
    "require_metadata_match": True,
    # Put the cover (Cover Art Archive / Open Library covers) at the top of the description.
    "include_cover_in_description": True,
    # MusicBrainz and Open Library ask API clients to identify themselves: put a contact in here,
    # e.g. "aaodf/1.0 ( you@example.com )".
    "metadata_user_agent": "aaodf/1.0 (+https://github.com/nazklx/aaodf)",
    "metadata_timeout_seconds": 30,
    "musicbrainz_url": "https://musicbrainz.org",
    "musicbrainz_request_interval": 1.1,    # seconds between MusicBrainz requests (they allow 1/s)
    "coverart_url": "https://coverartarchive.org",
    "openlibrary_url": "https://openlibrary.org",
    "openlibrary_covers_url": "https://covers.openlibrary.org",
    "openlibrary_request_interval": 1.0,    # seconds between Open Library requests
    # How book / audiobook folders are named when there are no usable tags:
    # "author_title" ("Author - Title (Year)") or "title_author".
    "book_name_order": "author_title",
    # Tracker ids for non-video uploads. 0 = not set: the upload is blocked until you set them
    # here or type them into the pre-upload form.
    "music_category_id": 0,
    "audiobook_category_id": 0,
    "book_category_id": 0,
    "music_type_id": 0,
    "audiobook_type_id": 0,
    "book_type_id": 0,
    "media_resolution_id": 0,     # resolution_id sent for non-video uploads; 0 = leave it out
    # Name of the extra upload form field that carries the MusicBrainz / Open Library link.
    # "" = no extra field; the link is always part of the description either way.
    "musicbrainz_link_field": "",
    "openlibrary_link_field": "",

    "screenshot_count": 18,
    "screenshot_start_seconds": 60,
    "screenshot_interval_seconds": 240
}

TYPE_IDS = {
    "disc": 1,
    "remux": 2,
    "encode": 3,
    "webdl": 4,
    "webrip": 5,
    "hdtv": 6,
}

RESOLUTION_IDS = {
    "4320p": 1, "2160p": 2, "1080p": 3, "1080i": 4, "720p": 5,
    "576p": 6, "576i": 7, "480p": 8, "480i": 9, "Other": 10,
}

MEDIA_TYPES = ("auto", "video", "music", "audiobook", "book")

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=4), encoding="utf-8")
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        config = DEFAULT_CONFIG.copy()
        config.update(data)
        return config
    except Exception:
        return DEFAULT_CONFIG.copy()

config = load_config()

ANNOUNCE_URL = config.get("announce_url", "#")
TMDB_API_KEY = config.get("tmdb_api_key", "#")
TRACKER_API_TOKEN = config.get("tracker_api_token", "#")
UPLOAD_ENDPOINT = config.get("upload_endpoint", "#")
TRACKER_SEARCH_ENDPOINT = config.get("tracker_search_endpoint", "").strip()
CONFIRM_BEFORE_UPLOAD = config.get("confirm_before_upload", True)
ANONYMOUS_UPLOAD = config.get("anonymous_upload", False)
PERSONAL_RELEASE = config.get("personal_release", False)
STREAM_OPTIMIZED = config.get("stream_optimized", False)
GROUP_SEPARATOR = config.get("group_separator", "-")
RUN_AS_USER = config.get("run_as_user", "").strip()

def get_screenshot_settings() -> tuple[int, int, int]:
    """Read screenshot settings for the active config, including per-job overrides."""
    data = load_config()

    def setting(key: str, default: int, minimum: int) -> int:
        value = data.get(key, default)
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed if parsed >= minimum else default

    return (
        setting("screenshot_count", 18, 1),
        setting("screenshot_start_seconds", 60, 0),
        setting("screenshot_interval_seconds", 240, 1),
    )
