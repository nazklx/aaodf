1. Rename config_exmaple.json to config.json and make your changes
2. Execute run.sh without arguments to build venv
3. ./run.sh <File/folder to upload>

## Web UI

    ./run_web.sh [--host 127.0.0.1] [--port 8765] [--root /] [--jobs 1]

Open the URL it prints (it contains a one-time access token). The UI lets you:

- **Files** – browse the filesystem, tick any files/folders and run the script on them. Ticking several makes a batch.
- **Jobs** – progress per job (frames, review, image hosting, upload), live log, concurrency limit, cancel/retry.
  A job pauses for you at the "delete unwanted images" step and again before the tracker upload.
- **Library** – view generated screenshots (lightbox, arrow keys), delete single images or whole output folders.
- **Settings** – edit `config.json` (secrets are write-only).

Per-run options (review pause, auto-upload, anonymous/personal/stream) are set when queueing and override `config.json` for that job only.
The server binds to localhost by default; use `--root` to limit what the browser may see. `run_as_user` is ignored by the web UI.
