1. Rename config_example.json to config.json and make your changes
2. Execute run.sh without arguments to build venv
3. `./run.sh <file or folder to upload>`
4. `./run.sh --scan` to scan a qBittorrent-style download directory for tracker duplicates and select unmatched items for batch processing.

## Web UI

    ./run_web.sh [--host 127.0.0.1] [--port 8765] [--root /] [--jobs 1]

Open the URL it prints (it contains a one-time access token). The UI lets you:

- **Files** – browse the filesystem, tick any files/folders and run the script on them. Ticking several makes a batch.
- **Duplicates** – choose a qBittorrent-style download directory, search every immediate subfolder and every top-level MKV/MP4 file against the tracker, review matches and batch-queue unmatched items.
- **Jobs** – progress per job (frames, review, image hosting, upload), live log, concurrency limit, cancel/retry.
  A job pauses for you at the "delete unwanted images" step and again before the tracker upload.
- **Library** – view generated screenshots (lightbox, arrow keys), delete single images or whole output folders.
- **Settings** – edit `config.json` (secrets are write-only), configure the tracker search endpoint and optional qBittorrent/Transmission download client, and set screenshot count, first timestamp and interval.

Per-run options (review pause, auto-upload, anonymous/personal/stream) are set when queueing and override `config.json` for that job only.
The server binds to localhost by default; use `--root` to limit what the browser may see. `run_as_user` is ignored by the web UI.

### Duplicate scanning

The Duplicates tab scans only the selected directory's immediate children: each subfolder is treated as one release (including videos nested inside it), while only top-level `.mkv` and `.mp4` files are scanned individually. Folders without supported video files are skipped. Any tracker request or response error stops the scan; items are not classified as unmatched after a partial scan. Select the unmatched entries to queue them as a batch.

`tracker_search_endpoint` is optional. When blank, AAODF derives the UNIT3D `/api/torrents/filter` endpoint from `upload_endpoint`.

### Screenshot settings

- `screenshot_count`: total screenshots generated per release (default `18`).
- `screenshot_start_seconds`: timestamp of the first screenshot for a single-video release (default `60`).
- `screenshot_interval_seconds`: spacing between screenshots for a single-video release (default `240`).

For multi-episode releases, `screenshot_count` is distributed across the usable episodes; timestamps are sampled within each episode.

### Download client

`download_client` may be `null` (download the returned torrent URL into the generated output folder), or an object with `type` (`qbittorrent` or `transmission`), `url`, optional `username`/`password`, and client-specific options. qBittorrent supports `category`, `tags`, and `save_path`; Transmission supports `download_dir`. These options are editable in WebUI Settings.
