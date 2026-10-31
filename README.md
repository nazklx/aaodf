
    ./run_web.sh [--host 127.0.0.1] [--port 8765] [--root /] [--jobs 1]

Open the URL it prints out at first that includes the token.
Traverse filesystem, add batch jobs, automatically grab keyframes at user-set intervals and allow you to review, Generate and send nfo file and raw mediainfo output, pull info from tmdb, automatically upload (with or without confirmation) generated torrent to tracker and send the tracker's torrent to your download client. Settings able to be changed within the webui.

Notes:
- Duplicate scanning does not work due to rate-limiting. 
- This is tailor-made for 0df, how I upload, and TV/MOVIES. 
- Vibe coded as hell and for free so expect some bugs.

Settings:

- `tracker_search_endpoint` is optional. When blank, AAODF derives the UNIT3D `/api/torrents/filter` endpoint from `upload_endpoint`.
- `screenshot_count`: total screenshots generated per release (default `18`).
- `screenshot_start_seconds`: timestamp of the first screenshot for a single-video release (default `60`).
- `screenshot_interval_seconds`: spacing between screenshots for a single-video release (default `240`).
- For multi-episode releases, `screenshot_count` is distributed across the usable episodes; timestamps are sampled within each episode.
- `download_client` may be `null` (download the returned torrent URL into the generated output folder), or an object with `type` (`qbittorrent` or `transmission`), `url`, optional `username`/`password`, and client-specific options. qBittorrent supports `category`, `tags`, and `save_path`; Transmission supports `download_dir`. These options are editable in WebUI Settings.
