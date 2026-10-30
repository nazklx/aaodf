from __future__ import annotations
import os
import shutil
import subprocess
import sys
import re
import threading
import requests
from pathlib import Path

from .core import find_videos, episode_videos, episode_info, extract_frames, pause
from .models import Job
from .tmdb import lookup_tmdb
from .tracker import (build_torrent, host_screenshots, print_description_block,
                      upload_release, update_description, search_tracker, tracker_search_title)

def read_mediainfo(video: Path) -> str:
    text = subprocess.run(["mediainfo", str(video)], capture_output=True,
                          text=True, check=False, stdin=subprocess.DEVNULL).stdout
    return re.sub(r"^(Complete name\s*:\s*).*$", lambda m: m.group(1) + video.name, text, flags=__import__("re").M)

def process_target(target: Path) -> bool:
    videos = find_videos(target)
    if not videos:
        print(f"Skipping: No .mkv/.mp4 files found in: {target}")
        return False
    sources = episode_videos(videos)
    first = sources[0]
    multi = len(sources) > 1

    for tool in ("ffmpeg", "ffprobe", "mediainfo"):
        if not shutil.which(tool):
            print(f"Error: {tool} is not installed.")
            return False

    release = target.name if target.is_dir() else first.stem
    name = release if multi else first.stem
    root_output_dir = Path("output")
    root_output_dir.mkdir(exist_ok=True)
    out_dir = root_output_dir / f"UPLOAD.{name}"
    out_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    job = Job(
        target=target, sources=sources, first=first, release=release, name=name,
        out_dir=out_dir, torrent_path=out_dir / f"{name}.torrent",
        nfo_path=out_dir / f"{name}.nfo",
        tv_hint=any(episode_info(v) for v in videos),
    )

    torrent_thread = threading.Thread(target=build_torrent, args=(job,), daemon=True)
    torrent_thread.start()
    extract_frames(job.sources, job.name, job.out_dir)
    pause("Delete unwanted images, remaining images will be uploaded. Press ENTER to continue...\n")
    if not host_screenshots(job):
        return False
    if torrent_thread.is_alive():
        torrent_thread.join()

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
    return upload_release(job, read_mediainfo(first), extract_frames_func=extract_frames)


def scan_download_directory(root: Path) -> list[Path] | None:
    if not root.is_dir():
        print(f"Error: Not a directory: {root}")
        return None

    candidates = sorted(
        [p for p in root.iterdir() if not p.name.startswith(".") and p.is_dir()],
        key=lambda p: p.name.casefold(),
    )
    candidates.extend(sorted(
        [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in (".mkv", ".mp4")],
        key=lambda p: p.name.casefold(),
    ))
    if not candidates:
        print("No folders or top-level .mkv/.mp4 files were found.")
        return []

    unmatched: list[Path] = []
    matched_count = 0
    skipped_count = 0
    print(f"Scanning {len(candidates)} folders/files in: {root}\n")
    for index, candidate in enumerate(candidates, 1):
        query = tracker_search_title(candidate.name)
        print(f"[{index}/{len(candidates)}] Searching: {candidate.name}")
        print(f"    Tracker query: {query}")
        try:
            matches = search_tracker(query)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            print(f"Tracker search failed: {exc}")
            print("Scan stopped; no items will be marked as unmatched from an incomplete scan.")
            return None
        if matches:
            matched_count += 1
            print(f"    Found {len(matches)} possible duplicate(s):")
            for match in matches[:5]:
                extra = []
                if match.get("resolution"):
                    extra.append(str(match["resolution"]))
                if match.get("type"):
                    extra.append(str(match["type"]))
                if match.get("seeders") is not None:
                    extra.append(f"{match['seeders']} seeders")
                suffix = f" ({', '.join(extra)})" if extra else ""
                print(f"      - {match['name']}{suffix}")
            if len(matches) > 5:
                print(f"      ... and {len(matches) - 5} more")
        else:
            print("    No tracker matches.")
            if candidate.is_dir() and not find_videos(candidate):
                skipped_count += 1
                print("    Skipping batch list: folder contains no supported .mkv/.mp4 media.")
            else:
                unmatched.append(candidate)

    print(f"\nScan complete: {matched_count} matched, {len(unmatched)} unmatched, {skipped_count} skipped.")
    return unmatched


def select_batch(items: list[Path]) -> list[Path]:
    if not items:
        print("Nothing to add.")
        return []
    print("\nItems with no tracker search results:")
    for i, path in enumerate(items, 1):
        kind = "folder" if path.is_dir() else "file"
        print(f"  {i:>3}) [{kind}] {path.name}")
    print("\nEnter 'all' to add everything, numbers/ranges (e.g. 1,3-5), or 0 to cancel.")
    choice = input("Selection: ").strip().lower()
    if choice in ("all", "a"):
        return items
    if not choice or choice == "0":
        return []
    selected: list[Path] = []
    try:
        for part in choice.split(","):
            part = part.strip()
            if "-" in part:
                first, last = (int(value.strip()) for value in part.split("-", 1))
                if first < 1 or last > len(items) or first > last:
                    raise ValueError
                selected.extend(items[first - 1:last])
            else:
                number = int(part)
                if number < 1 or number > len(items):
                    raise ValueError
                selected.append(items[number - 1])
    except ValueError:
        print("Invalid selection; nothing will be added.")
        return []
    return list(dict.fromkeys(selected))


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--scan":
        raw_directory = input("qBittorrent download directory to scan: ").strip().strip('"')
        if not raw_directory:
            print("No directory provided.")
            return 1
        directory = Path(raw_directory).expanduser().resolve()
        unmatched = scan_download_directory(directory)
        if unmatched is None:
            return 1
        selected = select_batch(unmatched)
        if not selected:
            print("No items selected.")
            return 0
        print(f"\nStarting batch of {len(selected)} item(s).")
        successes = 0
        for index, target in enumerate(selected, 1):
            print(f"\n{'=' * 60}\nBATCH ITEM {index}/{len(selected)}: {target.name}\n{'=' * 60}")
            successes += bool(process_target(target))
        print(f"\nBatch finished: {successes}/{len(selected)} uploaded successfully.")
        return 0 if successes == len(selected) else 1

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file or folder> | --scan")
        return 1
    target = Path(os.path.abspath(sys.argv[1]))
    if not target.exists():
        print(f"Error: Path does not exist: {target}")
        return 1
    return 0 if process_target(target) else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
