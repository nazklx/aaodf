This is made for hosting on your seedbox/server that's already seeding torrents from other trackers, upload them as new torrents to 0df, and start cross-seeding.

    ./run_web.sh [--host 127.0.0.1] [--port 8765] [--root /] [--jobs 1]

Open the URL it prints out at first that includes the token.

#### Description:
Traverse filesystem, add batch jobs (video, music, audiobooks, books), automatically grab keyframes at user-set intervals and allow you to review before uploading to host, Generate and send nfo file and raw mediainfo output, pull info from tmdb, automatically upload (with or without confirmation) generated torrent to tracker and send the tracker's torrent to your download client. Scan your entire download folder for duplicates. Settings able to be changed within the webui.

#### Music, audiobooks and books:
Besides video, folders (or single files) of music, audiobooks and ebooks can be queued. The type is auto-detected from the files (`.m4b`, or a folder named audiobook/unabridged/narrated, is an audiobook; other audio is music; epub/pdf/mobi/azw3/djvu/fb2 are books) or forced per job in the web UI, with `--type` on the command line, or with `media_type` in the config.
- **Music** is matched on [MusicBrainz](https://musicbrainz.org). If the files carry a `MUSICBRAINZ_ALBUMID` tag (Picard) that release is used directly, otherwise it searches by artist + album from the tags or the folder name (`Artist - Album (Year) [FLAC]`), preferring the right year and track count.
- **Audiobooks and books** are matched on [Open Library](https://openlibrary.org), by ISBN when one is known (EPUB metadata, tags, folder name), otherwise by title + author.
- The match link (`https://musicbrainz.org/release/<mbid>` or `https://openlibrary.org/works/<OL...W>`) is kept for the tracker: it is in the description that is uploaded, and, if you set `musicbrainz_link_field` / `openlibrary_link_field`, it is also sent as that extra form field. It shows in the pre-upload form (editable) and on the job.
- There are no screenshots for these types. The upload gets a generated name (`Artist - Album (Year) [FLAC 24-96]`), a text NFO with the file list and the link, the match details (and cover) as description, and the `mediainfo` of the first audio file.
- Category and type ids depend on your tracker, so set `music_*`, `audiobook_*` and `book_*` ids below (or type them into the pre-upload form); the upload is blocked while they are 0.
- With no match there is no link to give the tracker, so the upload is skipped unless `require_metadata_match` is `false`.
- Please put a contact in `metadata_user_agent`; both services ask API clients to identify themselves. MusicBrainz requests are spaced at least 1 second apart.

Extra config.json keys (all editable under Settings → Music, audiobooks and books):
```
{
    "media_type": "auto",                          // auto | video | music | audiobook | book
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
    "book_name_order": "author_title",             // or title_author (used when there are no tags)
    "music_category_id": 0,  "music_type_id": 0,
    "audiobook_category_id": 0,  "audiobook_type_id": 0,
    "book_category_id": 0,  "book_type_id": 0,
    "media_resolution_id": 0,                      // resolution_id for these uploads, 0 = leave out
    "musicbrainz_link_field": "",                  // extra upload form field for the link, "" = description only
    "openlibrary_link_field": ""
}
```
(The comments are only for this README; config.json must be plain JSON.) Non-video uploads send `tmdb=0` and no resolution unless `media_resolution_id` is set. Duplicate scans use the configured `media_type` (auto-detect by default).

#### Notes:
- This is tailor-made for one tracker, how I upload, and TV/MOVIES (plus the music/book support above).
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
}
```
Note: Running as the same user as qbittorrent may help with permissions. You can also create a new user that's in the same group as qbittorrent as well. Most settings can be changed in webui, best practice to set your default settings here.
