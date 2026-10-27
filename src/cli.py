from __future__ import annotations
import os
import shutil
import subprocess
import sys
import re
import threading
from pathlib import Path

from .core import find_videos, episode_videos, episode_info, extract_frames, pause
from .models import Job
from .tmdb import lookup_tmdb
from .tracker import build_torrent, host_screenshots, print_description_block, upload_release, update_description

def read_mediainfo(video: Path) -> str:
    text = subprocess.run(["mediainfo", str(video)], capture_output=True,
                          text=True, check=False, stdin=subprocess.DEVNULL).stdout
    return re.sub(r"^(Complete name\s*:\s*).*$", lambda m: m.group(1) + video.name, text, flags=__import__("re").M)

def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file or folder>")
        return 1

    target = Path(os.path.abspath(sys.argv[1]))
    videos = find_videos(target)
    if not videos:
        print(f"Error: No .mkv/.mp4 files found in: {target}")
        return 1
    sources = episode_videos(videos)
    first = sources[0]
    multi = len(sources) > 1

    for tool in ("ffmpeg", "ffprobe", "mediainfo"):
        if not shutil.which(tool):
            print(f"Error: {tool} is not installed.")
            return 1

    release = target.name if target.is_dir() else first.stem
    name = release if multi else first.stem
    ROOT_OUTPUT_DIR = Path("output")
    ROOT_OUTPUT_DIR.mkdir(exist_ok=True)
    out_dir = ROOT_OUTPUT_DIR / f"UPLOAD.{name}"
    out_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    job = Job(
        target=target, sources=sources, first=first, release=release, name=name,
        out_dir=out_dir, torrent_path=out_dir / f"{name}.torrent",
        nfo_path=out_dir / f"{name}.nfo",
        tv_hint=any(episode_info(v) for v in videos),
    )

    # Start .torrent creation asynchronously in the background
    torrent_thread = threading.Thread(target=build_torrent, args=(job,), daemon=True)
    torrent_thread.start()

    extract_frames(job.sources, job.name, job.out_dir)

    pause("Delete unwanted images, remaining images will be uploaded. Press ENTER to continue...\n")
    if not host_screenshots(job):
        return 0

    # Ensure background torrent creation has completed before proceeding to upload/description
    if torrent_thread.is_alive():
        torrent_thread.join() 

    # Display torrent details right before description and uploading
    print()
    print("=" * 40)
    print("TORRENT DETAILS:")
    print(f"File: {job.torrent_path.name}")
    if job.torrent_path.exists():
        size_mb = job.torrent_path.stat().st_size / (1024 * 1024)
        print(f"Size: {size_mb:.2f} MB")
        print(f"Path: {job.torrent_path}")
    else:
        print("Status: Torrent file creation failed or incomplete.")
    print("=" * 40)
    print()

    job.info = lookup_tmdb(job.release, tv_hint=job.tv_hint)
    update_description(job, overview=job.info.overview if job.info else "")
    print_description_block(job)

    print("Generating nfo")
    xml = subprocess.run(["mediainfo", "--Output=XML", str(first)],
                         capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout
    job.nfo_path.write_text(xml, encoding="utf-8")

    print()
    upload_release(job, read_mediainfo(first), extract_frames_func=extract_frames)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
