This is made for hosting on your seedbox/server that's already seeding torrents from other trackers, upload them as new torrents to 0df, and start cross-seeding.

    ./run_web.sh [--host 127.0.0.1] [--port 8765] [--root /] [--jobs 1]

Open the URL it prints out at first that includes the token.

#### Description:
Traverse filesystem, add batch jobs, automatically grab keyframes at user-set intervals and allow you to review before uploading to host, Generate and send nfo file and raw mediainfo output, pull info from tmdb, automatically upload (with or without confirmation) generated torrent to tracker and send the tracker's torrent to your download client. Now can scane your entire download folder for duplicates for no second guessing and easy batch selection. Settings able to be changed within the webui.

#### Notes:
- This is tailor-made for one tracker, how I upload, and TV/MOVIES. 
- Vibe coded as hell and for free so expect some bugs.

#### Settings:
- `tracker_search_endpoint` is optional. When blank, AAODF derives the UNIT3D `/api/torrents/filter` endpoint from `upload_endpoint`.
- `screenshot_count`: total screenshots generated per release (default `18`).
- `screenshot_start_seconds`: timestamp of the first screenshot for a single-video release (default `60`).
- `screenshot_interval_seconds`: spacing between screenshots for a single-video release (default `240`).
- For multi-episode releases, `screenshot_count` is distributed across the usable episodes; timestamps are sampled within each episode.
- `download_client` may be `null` (download the returned torrent URL into the generated output folder), or an object with `type` (`qbittorrent` or `transmission`), `url`, optional `username`/`password`, and client-specific options. qBittorrent supports `category`, `tags`, and `save_path`; Transmission supports `download_dir`.
- Promotion flags, sent with every upload: `free` (freeleech percentage, `0` = off, `1`-`100`), `free_days` (how long the freeleech lasts, `0` = never expires), `doubleup` (`true`/`false`), `doubleup_days` (`0` = never expires) and `featured` (`true`/`false`, sends both the tracker's `featured` and `sticky` flags). They are set in `config.json` as defaults, can be changed per job in the web UI's Files/Duplicates options, and can be edited again in the pre-upload check. The tracker ignores them unless your account is allowed to set them.
- `tracker_search_rate_limit`: tracker search requests per minute used by duplicate scans (default `30`; each page of results counts as one request).

#### Example config.json
```
{
    "announce_url": "https://tracker.com/announce/xayz4add",
    "tmdb_api_key": "Long API Read Access Token",
    "tracker_api_token": "API Token from your user settings in tracker",
    "upload_endpoint": "https://tracker.com/api/upload",
    "confirm_before_upload": true,
    "anonymous_upload": false,
    "personal_release": false,
    "stream_optimized": false,
    "free": 0,
    "free_days": 0,
    "doubleup": false,
    "doubleup_days": 0,
    "featured": false,
    "group_separator": "-",
    "run_as_user": "qbittorrent",
    "tracker_search_endpoint": "",
    "download_client": {
        "type": "qbittorrent",
        "url": "http://localhost:8080",
        "username": "admin",
        "category": "upload-tv",
        "tags": "",
        "save_path": "/qbittorrent/downloads/tv",
        "download_dir": "",
        "password": "password!"
    },
    "screenshot_count": 18,
    "screenshot_start_seconds": 60,
    "screenshot_interval_seconds": 240
}
```
Note: Running as the same user as qbittorrent may help with permissions. You can also create a new user that's in the same group as qbittorrent as well. Most settings can be changed in webui, best practice to set your default settings here.
