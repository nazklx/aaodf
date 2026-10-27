from __future__ import annotations
import json
from pathlib import Path

CONFIG_FILE = Path("config.json")

DEFAULT_CONFIG = {
    "announce_url": "#",
    "tmdb_api_key": "#",
    "tracker_api_token": "#",
    "upload_endpoint": "#",
    "confirm_before_upload": True,
    "anonymous_upload": False,
    "personal_release": False,
    "stream_optimized": False,
    "group_separator": "-",
    "run_as_user": ""
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
CONFIRM_BEFORE_UPLOAD = config.get("confirm_before_upload", True)
ANONYMOUS_UPLOAD = config.get("anonymous_upload", False)
PERSONAL_RELEASE = config.get("personal_release", False)
STREAM_OPTIMIZED = config.get("stream_optimized", False)
GROUP_SEPARATOR = config.get("group_separator", "-")
RUN_AS_USER = config.get("run_as_user", "").strip()
