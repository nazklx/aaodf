from __future__ import annotations
import os
import random
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .models import Job

from .config import get_screenshot_settings

VIDEO_EXTS = {".mkv", ".mp4"}
FRAME_WORKERS = 4

_EPISODE_RE = re.compile(r"(?<![A-Za-z0-9])S(\d{1,3})[ ._-]?E(\d{1,4})(?![0-9])", re.I)
_SAMPLE_RE = re.compile(r"(?:^|[._\- ])samples?(?:[._\- ]|$)", re.I)

def err(msg: str) -> None:
    print(msg, file=sys.stderr)

def episode_info(p: Path) -> tuple[int, int] | None:
    m = _EPISODE_RE.search(p.name)
    return (int(m.group(1)), int(m.group(2))) if m else None

def episode_label(p: Path) -> str:
    info = episode_info(p)
    return f"S{info[0]:02d}E{info[1]:02d}" if info else short_name(p, 24)

def _episode_sort_key(p: Path) -> tuple:
    info = episode_info(p)
    if info is None:
        return (1, 0, 0, str(p).lower())
    season, episode = info
    return (0, season or 10**6, episode, str(p).lower())

def find_videos(target: Path) -> list[Path]:
    if target.is_file():
        return [target] if target.suffix.lower() in VIDEO_EXTS else []
    videos = [p for p in target.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    real = [p for p in videos if not any(_SAMPLE_RE.search(part) for part in p.relative_to(target).parts)]
    files = real or videos
    if not any(episode_info(p) for p in files):
        return sorted(files, key=lambda p: (-p.stat().st_size, str(p).lower()))
    return sorted(files, key=_episode_sort_key)

def episode_videos(videos: list[Path]) -> list[Path]:
    marked = [v for v in videos if episode_info(v)]
    regular = [v for v in marked if episode_info(v)[0] != 0]
    return regular or marked or videos[:1]

def video_duration(video: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
            stdin=subprocess.DEVNULL,
        ).stdout.strip()
        return float(out)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None

def short_name(p: Path, width: int = 40) -> str:
    n = p.name
    return n if len(n) <= width else "…" + n[-(width - 1):]

def short_msg(text: object, width: int = 120) -> str:
    lines = str(text).strip().splitlines()
    msg = lines[-1].strip() if lines else ""
    return msg if len(msg) <= width else msg[: width - 1] + "…"

def pause(prompt: str) -> None:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        import termios, tty
    except ImportError:
        termios = tty = None
    if termios is None or not sys.stdin.isatty():
        sys.stdin.readline()
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
        tty.setcbreak(fd)
        while True:
            ch = os.read(fd, 1)
            if ch in (b"\r", b"\n", b""):
                break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    sys.stdout.write("\n")

def human_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TiB"

def extract_frame(video: Path, ts: int, out: Path) -> str | None:
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-skip_frame", "nokey", "-ss", str(ts), "-i", str(video),
         "-frames:v", "1", "-update", "1", str(out)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if r.returncode != 0 or not out.exists():
        return short_msg(r.stderr) or "no frame produced"
    return None

def plan_episode_frames(videos: list[Path], workers: int, frame_count: int) -> list[tuple[Path, int]]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        durations = list(pool.map(video_duration, videos))
    usable = [(v, d) for v, d in zip(videos, durations) if d and d > 60]
    n = len(usable)
    if n == 0:
        return []

    if n >= frame_count:
        picks = {n // 2} if frame_count == 1 else {
            round(i * (n - 1) / (frame_count - 1)) for i in range(frame_count)
        }
        counts = {i: 1 for i in picks}
    else:
        counts = {i: (frame_count * (i + 1)) // n - (frame_count * i) // n for i in range(n)}

    plan = []
    for i in sorted(counts):
        video, duration = usable[i]
        k = counts[i]
        lo, hi = duration * 0.08, duration * 0.92
        step = (hi - lo) / k
        for j in range(k):
            plan.append((video, int(lo + (j + random.random()) * step)))
    return plan

def extract_frames(videos: list[Path], name: str, out_dir: Path, jitter: bool = False) -> None:
    frame_count, first_frame_at, frame_step = get_screenshot_settings()
    workers = min(FRAME_WORKERS, os.cpu_count() or 1)
    jobs = []
    if len(videos) == 1:
        video = videos[0]
        duration = video_duration(video)
        for i in range(frame_count):
            ts = first_frame_at + i * frame_step
            if jitter:
                reach = max(0, frame_step // 2 - 20)
                ts = max(5, ts + random.randint(-reach, reach))
            if duration is not None and ts >= duration:
                continue
            out = out_dir / f"{name}_frame_{i + 1:02d}.png"
            jobs.append((i + 1, video, ts, out, f"at {ts}s"))
        print(f"Extracting {len(jobs)} frames ({workers} at a time)...")
    else:
        plan = plan_episode_frames(videos, workers, frame_count)
        for i, (video, ts) in enumerate(plan):
            out = out_dir / f"{name}_frame_{i + 1:02d}.png"
            jobs.append((i + 1, video, ts, out, f"{episode_label(video)} at {ts}s"))
        episodes = len({job[1] for job in jobs})
        print(f"Extracting {len(jobs)} random keyframes across {episodes} of {len(videos)} episodes ({workers} at a time)...")

    ok_count = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [(idx, label, pool.submit(extract_frame, video, ts, out))
                   for idx, video, ts, out, label in jobs]
        for idx, label, fut in futures:
            if fut.result() is None:
                ok_count += 1
                print(f"  Frame {idx:>2}/{len(jobs)} {label}: done")
            else:
                print(f"  Frame {idx:>2}/{len(jobs)} {label}: FAILED")
    print(f"Extracted {ok_count} frames to: {out_dir}")
