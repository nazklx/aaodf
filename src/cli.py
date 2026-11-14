from __future__ import annotations
import os
import shutil
import subprocess
import sys
import re
import threading
import requests
from pathlib import Path

from .config import config
from .core import find_videos, episode_videos, episode_info, extract_frames, pause
from .media import (KIND_LABELS, MEDIA_EXTS, build_nfo, detect_kind,
                    lookup_media, media_sources, media_tags, meta_description, normalize_hint)
from .models import Job
from .tmdb import lookup_tmdb
from .tracker import (build_torrent, host_screenshots, print_description_block,
                      upload_release, update_description, search_tracker, tracker_search_title)

def read_mediainfo(video: Path) -> str:
    text = subprocess.run(["mediainfo", str(video)], capture_output=True,
                          text=True, check=False, stdin=subprocess.DEVNULL).stdout
    return re.sub(r"^(Complete name\s*:\s*).*$", lambda m: m.group(1) + video.name, text, flags=__import__("re").M)

def print_torrent_details(job: Job) -> None:
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

def process_media_target(target: Path, kind: str) -> bool:
    """Music (MusicBrainz), audiobooks and books (Open Library): no screenshots, the source link
    of the match goes into the description and to the tracker."""
    sources = media_sources(target, kind)
    if not sources:
        print(f"Skipping: no {KIND_LABELS[kind].lower()} files found in: {target}")
        return False
    audio = kind in ("music", "audiobook")
    if audio and not shutil.which("ffprobe"):
        print("Error: ffprobe is not installed.")
        return False

    release = target.name if target.is_dir() else target.stem
    out_dir = Path("output") / f"UPLOAD.{release}"
    out_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    job = Job(
        target=target, sources=sources, first=sources[0], release=release, name=release,
        out_dir=out_dir, torrent_path=out_dir / f"{release}.torrent",
        nfo_path=out_dir / f"{release}.nfo", tv_hint=False, media_kind=kind,
    )
    print(f"Media type: {kind}")
    torrent_thread = threading.Thread(target=build_torrent, args=(job,), daemon=True)
    torrent_thread.start()

    job.tags = media_tags(kind, sources)
    job.meta = lookup_media(job)
    update_description(job, overview=meta_description(job.meta) if job.meta else "")
    if job.meta:
        print(f"Source link: {job.meta.url}")
    if torrent_thread.is_alive():
        torrent_thread.join()
    print_torrent_details(job)
    print_description_block(job)

    print("Generating nfo")
    job.nfo_path.write_text(build_nfo(job), encoding="utf-8")
    mediainfo_text = read_mediainfo(sources[0]) if audio and shutil.which("mediainfo") else ""
    print()
    return upload_release(job, mediainfo_text)

def process_target(target: Path, media_type: str | None = None) -> bool:
    kind = detect_kind(target, media_type or config.get("media_type", "auto"))
    if kind is None:
        print(f"Skipping: no supported video, music, audiobook or book files found in: {target}")
        return False
    if kind != "video":
        return process_media_target(target, kind)
    print("Media type: video")
    videos = find_videos(target)
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

    print_torrent_details(job)

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
        [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS],
        key=lambda p: p.name.casefold(),
    ))
    if not candidates:
        print("No folders or top-level media files (video, audio, books) were found.")
        return []

    unmatched: list[Path] = []
    matched_count = 0
    skipped_count = 0
    print(f"Scanning {len(candidates)} folders/files in: {root}\n")
    hint = normalize_hint(config.get("media_type", "auto"))
    for index, candidate in enumerate(candidates, 1):
        kind = detect_kind(candidate, hint) or "video"
        query = tracker_search_title(candidate.name, kind)
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
            if candidate.is_dir() and detect_kind(candidate, hint) is None:
                skipped_count += 1
                print("    Skipping batch list: folder contains no supported media.")
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
    args = sys.argv[1:]
    media_type = None
    if "--type" in args:
        i = args.index("--type")
        media_type = args[i + 1].lower() if i + 1 < len(args) else ""
        del args[i:i + 2]
        if media_type not in ("auto", "video", "music", "audiobook", "book"):
            print("--type must be one of: auto, video, music, audiobook, book")
            return 1
    if args and args[0] == "--scan":
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
            successes += bool(process_target(target, media_type))
        print(f"\nBatch finished: {successes}/{len(selected)} uploaded successfully.")
        return 0 if successes == len(selected) else 1

    if not args:
        print(f"Usage: {sys.argv[0]} [--type auto|video|music|audiobook|book] <file or folder> | --scan")
        return 1
    target = Path(os.path.abspath(args[0]))
    if not target.exists():
        print(f"Error: Path does not exist: {target}")
        return 1
    return 0 if process_target(target, media_type) else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
