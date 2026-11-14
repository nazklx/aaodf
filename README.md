This is made for hosting on your seedbox/server that's already seeding torrents from other trackers, upload them as new torrents to 0df, and start cross-seeding.

    ./run_web.sh [--host 127.0.0.1] [--port 8765] [--root /] [--jobs 1]

Open the URL it prints out at first that includes the token.

#### Description:
Traverse filesystem, add batch jobs (video, music, audiobooks, books), automatically grab keyframes at user-set intervals and allow you to review before uploading to host, Generate and send nfo file and raw mediainfo output, pull info from tmdb, automatically upload (with or without confirmation) generated torrent to tracker and send the tracker's torrent to your download client. Scan your entire download folder for duplicates. Settings able to be changed within the webui.

#### Notes:
- This is made for a specific tracker
- Vibe coded as hell and for free so expect some bugs.
  
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
    "media_type": "auto",
    "musicbrainz_enabled": true,
    "openlibrary_enabled": true,
    "require_metadata_match": true,
    "include_cover_in_description": true,
    "metadata_user_agent": "aaodf/1.0 ( you@example.com )",
    "metadata_timeout_seconds": 30,
    "musicbrainz_url": "https://musicbrainz.org",
    "musicbrainz_request_interval": 1.1,
    "coverart_url": "https://coverartarchive.org",
    "openlibrary_url": "https://openlibrary.org",
    "openlibrary_covers_url": "https://covers.openlibrary.org",
    "openlibrary_request_interval": 1.0,
    "book_name_order": "author_title",
    "music_category_id": 0,  "music_type_id": 0,
    "audiobook_category_id": 0,  "audiobook_type_id": 0,
    "book_category_id": 0,  "book_type_id": 0,
    "media_resolution_id": 0,
    "musicbrainz_link_field": "",
    "openlibrary_link_field": ""
}
```
Note: Running as the same user as qbittorrent may help with permissions. You can also create a new user that's in the same group as qbittorrent as well. Most settings can be changed in webui, best practice to set your default settings here.
