"""Re-extract screenshots for a job that is waiting for review.

Mirrors tracker.make_screenshots(): remove old PNGs, then extract with jitter so
single-video releases get different timestamps.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from src.core import extract_frames, find_videos, episode_videos


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    sources = episode_videos(find_videos(Path(a.target)))
    if not sources:
        print("No videos found")
        return 1
    for old in out.glob("*.png"):
        old.unlink()
    extract_frames(sources, a.name, out, jitter=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
