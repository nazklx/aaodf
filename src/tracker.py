from __future__ import annotations
import contextlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse
import requests
from torf import Torrent

from .config import (
    ANNOUNCE_URL, TRACKER_API_TOKEN, UPLOAD_ENDPOINT,
    CONFIRM_BEFORE_UPLOAD, ANONYMOUS_UPLOAD, PERSONAL_RELEASE,
    STREAM_OPTIMIZED, TYPE_IDS, RESOLUTION_IDS
)
from .models import Job, Plan
from .parsers import parse_tags, mediainfo_tags, tv_numbers, build_name, pick_type
from .tmdb import tmdb_by_id
from .core import short_msg, human_size, pause

try:
    import termios, tty
except ImportError:
    termios = tty = None

TYPE_LABELS = {"disc": "Full Disc", "remux": "Remux", "encode": "Encode",
               "webdl": "WEB-DL", "webrip": "WEBRip", "hdtv": "HDTV"}
SD_RESOLUTIONS = {"576p", "576i", "480p", "480i"}
_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*]')

def clean_title(name: str) -> str:
    return re.sub(r"\s+", " ", _FORBIDDEN_RE.sub("", name)).strip()[:255]

def compose_description(overview: str, links: list[str]) -> str:
    return "\n\n".join(p for p in (overview, "\n".join(links)) if p)

def update_description(job: Job, *, overview: str | None = None, links: list[str] | None = None) -> None:
    new_overview = job.overview if overview is None else overview
    new_links = job.links if links is None else links
    old_default = compose_description(job.overview, job.links)
    desc = job.description
    if desc in ("", old_default):
        desc = compose_description(new_overview, new_links)
    else:
        for label, old, new in (
            ("image links", "\n".join(job.links), "\n".join(new_links)),
            ("TMDB description", job.overview, new_overview),
        ):
            if old == new:
                continue
            if old and old in desc:
                desc = desc.replace(old, new, 1)
    job.description, job.overview, job.links = desc, new_overview, new_links

def get_category_id(is_tv: bool, multi: bool, tags: dict) -> int:
    resolution = tags.get("resolution") or ""
    source = tags.get("source") or ""
    video = tags.get("video") or ""
    remux = tags.get("remux", False)

    if is_tv:
        if multi or tags.get("is_pack", False):
            return 28
        if resolution == "2160p":
            return 22
        if resolution in ("480p", "480i", "576p", "576i"):
            return 21
        if source in ("BluRay", "BDRip", "BRRip"):
            return 23
        if source in ("DVD", "DVDRip"):
            return 25
        if video in ("H.264", "x264", "AVC"):
            return 30
        if video in ("H.265", "x265", "HEVC"):
            return 31
        if video in ("XviD", "DivX"):
            return 32
        if source in ("WEB-DL", "WEBRip", "WEB"):
            return 59
        return 30
    else:
        if multi or tags.get("is_pack", False):
            return 17
        if resolution == "2160p":
            return 10
        if resolution in ("480p", "480i", "576p", "576i"):
            return 9
        if remux or source in ("BluRay-Full", "Full Disc"):
            return 12
        if source in ("BluRay", "BDRip", "BRRip"):
            return 11
        if source == "DVD":
            return 14
        if video in ("H.264", "x264", "AVC"):
            return 18
        if video in ("H.265", "x265", "HEVC"):
            return 19
        if video in ("XviD", "DivX"):
            return 20
        if source in ("WEB-DL", "WEBRip", "WEB"):
            return 58
        return 18

def compute_plan(job: Job, mediainfo_text: str) -> Plan | None:
    from .tmdb import parse_release
    if not TRACKER_API_TOKEN.strip():
        print("TRACKER_API_TOKEN is empty - skipping tracker upload.")
        return None
    info = job.info
    if info is None or not info.id:
        print("No TMDB match, so there is no TMDB id to send - skipping tracker upload.")
        return None

    if job.tags is None:
        tags = parse_tags(job.release, job.first.stem)
        for key, value in mediainfo_tags(job.first).items():
            tags[key] = tags.get(key) or value
        job.tags = tags
    tags = job.tags

    tv = tv_numbers(job.sources)
    is_tv = tv is not None or info.kind == "tv"
    if is_tv and tv is None:
        print("This looks like TV but no SxxExx numbers were found in file names - skipping tracker upload.")
        return None
    if is_tv != (info.kind == "tv"):
        print(f"File types mismatch TMDB match ({info.label}) - skipping tracker upload.")
        return None

    year = parse_release(job.release)[1] or info.year
    name = clean_title(build_name(info.title, year, tv[2] if tv else None, tags))
    resolution = tags["resolution"] or "Other"
    type_key, type_known = pick_type(tags)
    multi = len(job.sources) > 1
    fields = {
        "name": name,
        "description": job.description or name,
        "mediainfo": mediainfo_text,
        "category_id": get_category_id(is_tv, multi, tags),
        "type_id": TYPE_IDS[type_key],
        "resolution_id": RESOLUTION_IDS.get(resolution, RESOLUTION_IDS["Other"]),
        "tmdb": info.id,
        "anonymous": int(ANONYMOUS_UPLOAD),
        "personal_release": int(PERSONAL_RELEASE),
        "stream": int(STREAM_OPTIMIZED),
        "sd": int(resolution in SD_RESOLUTIONS),
    }
    if info.imdb_id:
        fields["imdb"] = info.imdb_id
    if tv:
        if info.tvdb_id:
            fields["tvdb"] = info.tvdb_id
        fields["season_number"], fields["episode_number"] = tv[0], tv[1]
    missing = [k for k in ("resolution", "source", "audio", "video", "group") if not tags[k]]
    return Plan(fields, is_tv, tv, type_key, type_known, info, missing)

def recompute_plan(job: Job, old: Plan, mediainfo_text: str) -> Plan | None:
    new = compute_plan(job, mediainfo_text)
    if new is None:
        return None
    for key in old.edited:
        new.fields[key] = old.fields[key]
    new.edited = set(old.edited)
    return new

def _label_for(mapping: dict, value: int, labels: dict | None = None) -> str:
    key = next((k for k, v in mapping.items() if v == value), None)
    return (labels or {}).get(key, key) if key else "?"

def print_plan(plan: Plan) -> None:
    f = plan.fields
    note = ""
    if not plan.type_known and "type_id" not in plan.edited:
        note = "  <- source not recognised, assumed Encode; check!"
    desc = f["description"]
    print()
    print("Upload details:")
    print(f"  Name:         {f['name']}")
    print(f"  Category:     {'TV' if plan.is_tv else 'Movie'} (id {f['category_id']})")
    print(f"  Type:         {_label_for(TYPE_IDS, f['type_id'], TYPE_LABELS)} (id {f['type_id']}){note}")
    print(f"  Resolution:   {_label_for(RESOLUTION_IDS, f['resolution_id'])} (id {f['resolution_id']})")
    print(f"  TMDB match:   {plan.info.label}")
    print(f"  Description:  {len(desc)} characters, {desc.count('[img]')} image link(s)")

def post_upload(fields: dict, torrent_path: Path, nfo_path: Path) -> bool:
    headers = {"Authorization": f"Bearer {TRACKER_API_TOKEN.strip()}", "Accept": "application/json"}
    try:
        with torrent_path.open("rb") as tf, nfo_path.open("rb") as nf:
            r = requests.post(
                UPLOAD_ENDPOINT, headers=headers, data=fields,
                files={"torrent": (torrent_path.name, tf, "application/x-bittorrent"),
                       "nfo": (nfo_path.name, nf, "text/plain")},
                timeout=300,
            )
    except (requests.RequestException, OSError) as e:
        print(f"Upload failed: {short_msg(e) or type(e).__name__}")
        return False
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.ok and isinstance(body, dict) and body.get("success"):
        print("Upload successful.")
        return True
    print(f"Upload failed (HTTP {r.status_code})")
    return False

def read_key(valid: str, eof: str = "0") -> str:
    valid = valid.lower()
    if termios is None or not sys.stdin.isatty():
        while True:
            line = sys.stdin.readline()
            if not line:
                return eof
            key = line.strip()[:1].lower()
            if key in valid:
                return key
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
        tty.setcbreak(fd)
        while True:
            data = os.read(fd, 1)
            if not data:
                key = eof
                break
            key = data.decode("latin-1").lower()
            if key in valid:
                break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    sys.stdout.write(key + "\n")
    sys.stdout.flush()
    return key

def ask_line(prompt: str, prefill: str = "") -> str:
    try:
        import readline
    except ImportError:
        readline = None
    if readline and prefill:
        readline.set_startup_hook(lambda: readline.insert_text(prefill))
    try:
        return input(prompt).strip()
    except EOFError:
        return ""
    finally:
        if readline and prefill:
            readline.set_startup_hook()

def edit_in_editor(text: str) -> str | None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    cmd = shlex.split(editor) if editor else next(([c] for c in ("nano", "vim", "vi", "notepad") if shutil.which(c)), None)
    if not cmd:
        print("No text editor found.")
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(text)
        path = tf.name
    try:
        subprocess.run(cmd + [path], check=False)
        return Path(path).read_text(encoding="utf-8").strip("\n")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)

def build_torrent(job: Job) -> None:
    print(f"Creating .torrent file from: {job.target}")
    torrent = Torrent(path=job.target, trackers=[ANNOUNCE_URL], private=True)
    torrent.generate(callback=lambda _t, _p, done, total: print(f"\r  Hashing pieces: {done}/{total}", end="", flush=True), interval=0.25)
    print()
    torrent.write(job.torrent_path, overwrite=True)
    print("Torrent created")

def make_screenshots(job: Job, jitter: bool = False, extract_frames_func=None) -> None:
    for old in job.out_dir.glob("*.png"):
        old.unlink()
    if extract_frames_func:
        extract_frames_func(job.sources, job.name, job.out_dir, jitter=jitter)

def host_screenshots(job: Job) -> bool:
    from .uploaders import upload_image
    from .core import short_name
    import sys
    from pathlib import Path

    files = sorted(job.out_dir.glob("*.png"))
    if not files:
        print(f"No PNG files found in: {job.out_dir}", file=sys.stderr)
        return False

    total = len(files)
    links: list[str] = []
    failed: list[Path] = []
    
    for n, f in enumerate(files, 1):
        print(f"[{n}/{total}] {short_name(f)}", flush=True)
        bb, log = upload_image(f)
        print("\n".join(log), flush=True)
        if bb:
            links.append(bb)
        else:
            failed.append(f)

    print(f"\nUploaded {len(links)}/{total} images.")
    if failed:
        print("Failed: " + ", ".join(short_name(f, 24) for f in failed))
    update_description(job, links=links)
    return True

def print_description_block(job: Job) -> None:
    print()
    if job.overview:
        print(job.overview)
        print()
    print("\n".join(job.links))

def upload_release(job: Job, mediainfo_text: str, extract_frames_func=None) -> bool:
    plan = compute_plan(job, mediainfo_text)
    if plan is None:
        return False
    if not CONFIRM_BEFORE_UPLOAD:
        return post_upload(plan.fields, job.torrent_path, job.nfo_path)

    while True:
        print_plan(plan)
        print("\n  1) Upload now\n  0) Cancel")
        key = read_key("01")
        if key == "1":
            if post_upload(plan.fields, job.torrent_path, job.nfo_path):
                return True
        elif key == "0":
            return False
