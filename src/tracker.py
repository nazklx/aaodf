from __future__ import annotations
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit
import requests
from torf import Torrent

from .config import (
    ANNOUNCE_URL, TRACKER_API_TOKEN, UPLOAD_ENDPOINT, TRACKER_SEARCH_ENDPOINT,
    CONFIRM_BEFORE_UPLOAD, ANONYMOUS_UPLOAD, PERSONAL_RELEASE,
    STREAM_OPTIMIZED, TYPE_IDS, RESOLUTION_IDS, config, load_config
)
from .models import Job, Plan
from .parsers import parse_tags, mediainfo_tags, tv_numbers, build_name, pick_type
from .tmdb import tmdb_by_id, parse_release
from .core import short_msg, human_size, pause
from .media import KIND_LABELS, SOURCE_LABELS, SOURCE_OF_KIND, build_media_name, media_tags

try:
    import termios, tty
except ImportError:
    termios = tty = None

TYPE_LABELS = {"disc": "Full Disc", "remux": "Remux", "encode": "Encode",
               "webdl": "WEB-DL", "webrip": "WEBRip", "hdtv": "HDTV"}
SD_RESOLUTIONS = {"576p", "576i", "480p", "480i"}
_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*]')

# Every category id get_category_id() can return, with a label for the pre-upload editor.
CATEGORY_CHOICES = [
    (9, "Movie · SD"), (10, "Movie · 2160p"), (11, "Movie · BluRay"), (12, "Movie · Remux / Full Disc"),
    (14, "Movie · DVD"), (17, "Movie · Pack"), (18, "Movie · x264"), (19, "Movie · x265"),
    (20, "Movie · XviD"), (58, "Movie · WEB"), 
    (43, "Anime"), (61, "Anime · Packs"), (62, "Anime · Movies"),
    (38, "Music · Audio"), (39, "Music · FLAC"), (41, "Music · Packs"),
    (44, "Audiobooks"), (46, "Books"),
    (21, "TV · SD"), (22, "TV · 2160p"), (23, "TV · BluRay"), (25, "TV · DVD"), (28, "TV · Pack"),
    (30, "TV · x264"), (31, "TV · x265"), (32, "TV · XviD"), (59, "TV · WEB"),
]

# Promotion fields. `free` is a percentage (0 = off); the *_days values are how long it lasts
# (0 = forever). They live in Plan.fields under these names and are translated to the tracker's
# own field names (fl_until, du_until, ...) by tracker_payload().
PROMO_KEYS = ("free", "free_days", "doubleup", "doubleup_days", "featured")
MAX_PROMO_DAYS = 3650

# Non-video uploads (music / audiobook / book) carry the MusicBrainz / Open Library link in
# Plan.fields["source_url"] (+ "source_site"). These two never go to the tracker as-is: the link is
# sent in the description and, when `musicbrainz_link_field` / `openlibrary_link_field` is set in the
# config, also as that extra form field (see tracker_payload()).
INTERNAL_KEYS = ("source_url", "source_site")
MAX_URL_LEN = 500
_URL_RE = re.compile(r"^https?://\S+$")

def clean_title(name: str) -> str:
    return re.sub(r"\s+", " ", _FORBIDDEN_RE.sub("", name)).strip()[:255]

def is_video(kind: str) -> bool:
    return kind == "video"

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

def _config_int(key: str, lo: int, hi: int) -> int:
    value = config.get(key, 0)
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if lo <= number <= hi else 0

def promo_defaults() -> dict:
    """Free / double upload / featured as configured (per-job overrides arrive via AAODF_CONFIG)."""
    free = _config_int("free", 0, 100)
    doubleup = int(bool(config.get("doubleup", False)))
    return {
        "free": free,
        "free_days": _config_int("free_days", 0, MAX_PROMO_DAYS) if free else 0,
        "doubleup": doubleup,
        "doubleup_days": _config_int("doubleup_days", 0, MAX_PROMO_DAYS) if doubleup else 0,
        "featured": int(bool(config.get("featured", False))),
    }

def tracker_payload(fields: dict) -> dict:
    """Turn Plan.fields into the form data the tracker's upload API expects."""
    data = {k: v for k, v in fields.items() if k not in PROMO_KEYS + INTERNAL_KEYS and v is not None}
    for key in ("imdb", "tvdb"):          # optional ids: leave them out instead of sending 0
        if not data.get(key):
            data.pop(key, None)
    if not data.get("mediainfo"):         # books have none
        data.pop("mediainfo", None)
    link, site = str(fields.get("source_url") or ""), str(fields.get("source_site") or "")
    link_field = str(load_config().get(f"{site}_link_field", "") or "").strip() if site else ""
    if link and link_field:
        data[link_field] = link
    if fields.get("free"):
        data["free"] = int(fields["free"])
        if fields.get("free_days"):
            data["fl_until"] = int(fields["free_days"])
    if fields.get("doubleup"):
        data["doubleup"] = 1
        if fields.get("doubleup_days"):
            data["du_until"] = int(fields["doubleup_days"])
    if fields.get("featured"):
        data["featured"] = 1
        data["sticky"] = 1
    return data

def _media_id(key: str) -> int:
    return _config_int(key, 0, 99_999_999)

def compute_media_plan(job: Job, mediainfo_text: str) -> Plan | None:
    """Upload plan for music (MusicBrainz) and books / audiobooks (Open Library)."""
    kind, meta = job.media_kind, job.meta
    source = SOURCE_OF_KIND[kind]
    if meta is None and config.get("require_metadata_match", True):
        print(f"No {SOURCE_LABELS[source]} match, so there is no source link to send - skipping tracker upload.")
        return None
    if job.tags is None:
        job.tags = media_tags(kind, job.sources)
    name = clean_title(build_media_name(job))
    fields = {
        "name": name,
        "description": job.description or name,
        "mediainfo": mediainfo_text,
        "category_id": _media_id(f"{kind}_category_id"),
        "type_id": _media_id(f"{kind}_type_id"),
        "tmdb": 0,
        "anonymous": int(ANONYMOUS_UPLOAD),
        "personal_release": int(PERSONAL_RELEASE),
        "stream": 0,
        "sd": 0,
        "source_url": meta.url if meta else "",
        "source_site": source if meta else "",
        **promo_defaults(),
    }
    if resolution := _media_id("media_resolution_id"):
        fields["resolution_id"] = resolution
    return Plan(fields, False, None, kind, True, meta, [], media_kind=kind)

def plan_problems(plan: Plan) -> list[str]:
    """Things that must be fixed before this plan can be uploaded (non-video ids come from the config)."""
    if is_video(plan.media_kind):
        return []
    problems = []
    for key, label in (("category_id", "Category"), ("type_id", "Type")):
        if int(plan.fields.get(key) or 0) < 1:
            problems.append(f"{label} id is not set - set {plan.media_kind}_{key} in the config or enter it in the upload form")
    return problems

def compute_plan(job: Job, mediainfo_text: str) -> Plan | None:
    from .tmdb import parse_release
    if not is_video(job.media_kind):
        return compute_media_plan(job, mediainfo_text)
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
        **promo_defaults(),
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

_FORM_KEYS = ("name", "description", "category_id", "type_id", "resolution_id", "tmdb",
              "anonymous", "personal_release", "stream", "sd")
# Marker line the web UI looks for; the JSON after it describes the editable form.
PLAN_FIELDS_MARKER = "PLAN_FIELDS "

def plan_form(plan: Plan) -> dict:
    """The fields the web UI may edit before upload (mediainfo is deliberately left out)."""
    f = plan.fields
    form = {k: f[k] for k in _FORM_KEYS + PROMO_KEYS if k in f}   # resolution_id is absent for non-video
    if not is_video(plan.media_kind):
        form["source_url"] = f.get("source_url", "")
    form["imdb"] = f.get("imdb", 0)
    form["tvdb"] = f.get("tvdb", 0)
    if plan.tv:
        form["season_number"] = f["season_number"]
        form["episode_number"] = f["episode_number"]
    return form

def plan_choices() -> dict:
    return {
        "types": [[TYPE_IDS[k], TYPE_LABELS[k]] for k in TYPE_IDS],
        "resolutions": [[v, k] for k, v in RESOLUTION_IDS.items()],
        "categories": [[i, label] for i, label in CATEGORY_CHOICES],
    }

def _whole(value: object, lo: int, hi: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and lo <= value <= hi:
        return value
    return None

_ID_EDITS = {  # key -> (label, min, max)
    "category_id": ("Category", 1, 9999), "tmdb": ("TMDB id", 1, 99999999),
    "imdb": ("IMDb id", 0, 99999999), "tvdb": ("TVDB id", 0, 99999999),
    "season_number": ("Season", 0, 9999), "episode_number": ("Episode", 0, 99999),
    "free": ("Free percentage", 0, 100), "free_days": ("Free days", 0, MAX_PROMO_DAYS),
    "doubleup_days": ("Double upload days", 0, MAX_PROMO_DAYS),
}
_FLAG_EDITS = {"anonymous": "Anonymous", "personal_release": "Personal release", "stream": "Stream optimised",
               "sd": "SD", "doubleup": "Double upload", "featured": "Featured"}

def apply_edits(plan: Plan, raw: str) -> list[str]:
    """Apply one JSON object of field edits from the web UI.

    Returns a list of problems. The plan is only changed when that list is empty, so a bad
    value can never half-apply."""
    try:
        edits = json.loads(raw)
    except ValueError:
        return ["the edits were not valid JSON"]
    if not isinstance(edits, dict):
        return ["the edits must be a JSON object"]

    errors: list[str] = []
    staged: dict = {}
    for key, value in edits.items():
        if key == "name":
            text = value.strip() if isinstance(value, str) else ""
            if not text or len(text) > 255:
                errors.append("Name must be 1-255 characters")
            elif _FORBIDDEN_RE.search(text):
                errors.append('Name cannot contain < > : " / \\ | ? *')
            else:
                staged[key] = text
        elif key == "description":
            if not isinstance(value, str) or len(value) > 200_000:
                errors.append("Description must be text")
            else:
                staged[key] = value.strip() or plan.fields["name"]
        elif key == "type_id":
            if not is_video(plan.media_kind):
                if (number := _whole(value, 1, 10**6)) is None:
                    errors.append("Type must be a whole number from 1 to 1000000")
                else:
                    staged[key] = number
            elif _whole(value, 0, 10**6) not in TYPE_IDS.values():
                errors.append("Type is not one of the known types")
            else:
                staged[key] = int(value)
        elif key == "source_url":
            if is_video(plan.media_kind):
                errors.append("Source link only applies to music, audiobooks and books")
            elif not isinstance(value, str) or len(value.strip()) > MAX_URL_LEN or (
                    value.strip() and not _URL_RE.match(value.strip())):
                errors.append("Source link must be an http(s) URL")
            else:
                staged[key] = value.strip()
        elif key == "resolution_id":
            if not is_video(plan.media_kind):
                errors.append("Resolution only applies to video")
            elif _whole(value, 0, 10**6) not in RESOLUTION_IDS.values():
                errors.append("Resolution is not one of the known resolutions")
            else:
                staged[key] = int(value)
        elif key in _ID_EDITS:
            label, lo, hi = _ID_EDITS[key]
            if key in ("tmdb", "imdb", "tvdb") and not is_video(plan.media_kind):
                errors.append(f"{label} only applies to video")
            elif key in ("season_number", "episode_number") and not plan.tv:
                errors.append(f"{label} only applies to TV releases")
            elif (number := _whole(value, lo, hi)) is None:
                errors.append(f"{label} must be a whole number from {lo} to {hi}")
            else:
                staged[key] = number
        elif key in _FLAG_EDITS:
            if isinstance(value, bool) or value in (0, 1):
                staged[key] = int(bool(value))
            else:
                errors.append(f"{_FLAG_EDITS[key]} must be on or off")
        else:
            errors.append(f"Unknown field: {key}")
    if errors:
        return errors

    if "source_url" in staged:      # keep the link in the description in step with the edited link
        old_url, new_url = plan.fields.get("source_url", ""), staged["source_url"]
        text = staged.get("description", plan.fields["description"])
        if old_url and old_url != new_url and old_url in text:
            staged["description"] = text.replace(old_url, new_url)
    plan.fields.update(staged)
    plan.edited.update(staged)
    if not plan.fields.get("free"):
        plan.fields["free_days"] = 0
    if not plan.fields.get("doubleup"):
        plan.fields["doubleup_days"] = 0
    return []

def _days_text(days: int) -> str:
    return f"{days} day{'s' if days != 1 else ''}" if days else "no expiry"

def print_media_plan(plan: Plan) -> None:
    f = plan.fields
    desc = f["description"]
    if not sys.stdin.isatty():
        print(PLAN_FIELDS_MARKER + json.dumps(
            {"fields": plan_form(plan), "choices": plan_choices(), "is_tv": False,
             "media_kind": plan.media_kind}), flush=True)
    flags = [label for key, label in (("anonymous", "anonymous"), ("personal_release", "personal release")) if f.get(key)]
    link_field = str(load_config().get(f"{f.get('source_site', '')}_link_field", "") or "").strip()
    print()
    print("Upload details:")
    print(f"  Name:         {f['name']}")
    print(f"  Kind:         {KIND_LABELS.get(plan.media_kind, plan.media_kind)}")
    print(f"  Category:     id {f['category_id'] or 'NOT SET'}")
    print(f"  Type:         id {f['type_id'] or 'NOT SET'}")
    if f.get("resolution_id"):
        print(f"  Resolution:   id {f['resolution_id']}")
    if plan.info:
        print(f"  Match:        {plan.info.source_name}: {plan.info.label}")
    print(f"  Source link:  {f['source_url'] or 'none'}"
          + (f"  (also sent as form field '{link_field}')" if f["source_url"] and link_field else ""))
    print(f"  Flags:        {', '.join(flags) or 'none'}")
    print(f"  Free:         {str(f['free']) + '% for ' + _days_text(f['free_days']) if f['free'] else 'no'}")
    print(f"  Double up:    {_days_text(f['doubleup_days']) if f['doubleup'] else 'no'}")
    print(f"  Featured:     {'yes (sticky)' if f['featured'] else 'no'}")
    print(f"  Description:  {len(desc)} characters")
    for problem in plan_problems(plan):
        print(f"  WARNING:      {problem}")

def print_plan(plan: Plan) -> None:
    if not is_video(plan.media_kind):
        return print_media_plan(plan)
    f = plan.fields
    note = ""
    if not plan.type_known and "type_id" not in plan.edited:
        note = "  <- source not recognised, assumed Encode; check!"
    desc = f["description"]
    if not sys.stdin.isatty():      # only the web UI reads this line
        print(PLAN_FIELDS_MARKER + json.dumps(
            {"fields": plan_form(plan), "choices": plan_choices(), "is_tv": plan.is_tv}), flush=True)
    ids = [f"TMDB {f['tmdb']}"]
    if f.get("imdb"):
        ids.append(f"IMDb {f['imdb']}")
    if f.get("tvdb"):
        ids.append(f"TVDB {f['tvdb']}")
    flags = [label for key, label in (("anonymous", "anonymous"), ("personal_release", "personal release"),
                                      ("stream", "stream optimised"), ("sd", "SD")) if f.get(key)]
    print()
    print("Upload details:")
    print(f"  Name:         {f['name']}")
    print(f"  Category:     {'TV' if plan.is_tv else 'Movie'} (id {f['category_id']})")
    print(f"  Type:         {_label_for(TYPE_IDS, f['type_id'], TYPE_LABELS)} (id {f['type_id']}){note}")
    print(f"  Resolution:   {_label_for(RESOLUTION_IDS, f['resolution_id'])} (id {f['resolution_id']})")
    print(f"  TMDB match:   {plan.info.label}")
    print(f"  IDs:          {', '.join(ids)}")
    if plan.tv:
        print(f"  Season/Ep:    {f['season_number']} / {f['episode_number']}")
    print(f"  Flags:        {', '.join(flags) or 'none'}")
    print(f"  Free:         {str(f['free']) + '% for ' + _days_text(f['free_days']) if f['free'] else 'no'}")
    print(f"  Double up:    {_days_text(f['doubleup_days']) if f['doubleup'] else 'no'}")
    print(f"  Featured:     {'yes (sticky)' if f['featured'] else 'no'}")
    print(f"  Description:  {len(desc)} characters, {desc.count('[img]')} image link(s)")

def _tracker_search_url() -> str:
    current = load_config()
    configured = str(current.get("tracker_search_endpoint", "")).strip()
    if configured:
        return configured
    parsed = urlsplit(str(current.get("upload_endpoint", UPLOAD_ENDPOINT)).strip())
    path = parsed.path.rstrip("/")
    if path.endswith("/upload"):
        path = path[:-len("/upload")] + "/filter"
    elif path.endswith("/api/torrents"):
        path += "/filter"
    else:
        path = "/api/torrents/filter"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def tracker_search_title(label: str, kind: str = "video") -> str:
    from .media import MEDIA_EXTS
    raw = Path(label).stem if Path(label).suffix.lower() in MEDIA_EXTS else label
    title, year, _ = parse_release(raw)
    title = re.sub(r"[._]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -._")
    # Music / book names put the year in brackets ("Album (2020) [FLAC]"), so a "Title 2020" query would miss them.
    query = f"{title} {year}" if title and year and is_video(kind) else title
    return query or label


class SearchCancelled(Exception):
    """Raised inside search_tracker when the caller sets its cancel event."""


DEFAULT_SEARCH_RATE = 30      # requests per minute
MAX_SEARCH_ATTEMPTS = 5

# One shared slot clock for every search in this process, so concurrent callers can't
# add up to more than the configured rate. The rate is re-read from the config on every
# request, so a change made in the web UI applies to a scan that is already running.
_throttle_lock = threading.Lock()
_next_request_at = 0.0


def _search_interval() -> float:
    raw = load_config().get("tracker_search_rate_limit", DEFAULT_SEARCH_RATE)
    try:
        rate = float(raw)
        if rate != rate:  # NaN
            raise ValueError
    except (TypeError, ValueError):
        rate = DEFAULT_SEARCH_RATE
    return 60.0 / min(max(rate, 1.0), 600.0)


def _sleep(seconds: float, cancel: threading.Event | None) -> None:
    if cancel is not None:
        if cancel.wait(seconds):
            raise SearchCancelled()
    else:
        time.sleep(seconds)


def _throttle(cancel: threading.Event | None = None) -> None:
    """Block until this caller's request slot arrives (first request goes out immediately)."""
    global _next_request_at
    interval = _search_interval()
    with _throttle_lock:
        now = time.monotonic()
        start = max(now, _next_request_at)
        _next_request_at = start + interval
    if start > now:
        _sleep(start - now, cancel)


def _back_off(seconds: float) -> None:
    """Push the shared slot clock back (after a 429/5xx) so every caller waits."""
    global _next_request_at
    with _throttle_lock:
        _next_request_at = max(_next_request_at, time.monotonic() + seconds)


def _retry_after(response: requests.Response) -> float | None:
    try:
        return min(max(float(response.headers.get("Retry-After", "")), 1.0), 120.0)
    except (TypeError, ValueError):
        return None


def _tracker_get(endpoint: str, headers: dict, params: dict,
                 cancel: threading.Event | None = None) -> requests.Response:
    """Rate-limited GET that backs off and retries on 429, 5xx and connection errors."""
    last: Exception | None = None
    for attempt in range(MAX_SEARCH_ATTEMPTS):
        _throttle(cancel)
        delay = min(5.0 * 2 ** attempt, 60.0)
        try:
            response = requests.get(endpoint, headers=headers, params=params, timeout=45)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
        else:
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()   # other 4xx (bad token...) are not retried
                return response
            last = requests.HTTPError(f"HTTP {response.status_code} from tracker search", response=response)
            delay = _retry_after(response) or delay
        if attempt + 1 < MAX_SEARCH_ATTEMPTS:
            _back_off(delay)
    raise last if last else RuntimeError("Tracker search failed")


def search_tracker(title: str, cancel: threading.Event | None = None) -> list[dict]:
    current = load_config()
    token = str(current.get("tracker_api_token", "")).strip()
    if not token or token == "#":
        raise RuntimeError("TRACKER_API_TOKEN is empty; tracker duplicate search cannot run.")
    endpoint = _tracker_search_url()
    if not endpoint.startswith(("http://", "https://")):
        raise RuntimeError("Set upload_endpoint or tracker_search_endpoint in config.json before scanning.")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    matches: list[dict] = []
    page = 1
    while page <= 20:
        response = _tracker_get(endpoint, headers,
                                {"name": title, "perPage": 100, "page": page}, cancel)
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Tracker search returned an unexpected response format.")
        data = body.get("data", [])
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError("Tracker search response did not contain a result list.")
        for item in data:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else item
            matches.append({
                "id": item.get("id") or attrs.get("id"),
                "name": attrs.get("name") or attrs.get("title") or "Unknown torrent",
                "seeders": attrs.get("seeders"),
                "resolution": attrs.get("resolution"),
                "type": attrs.get("type"),
            })
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        last_page = int(meta.get("last_page") or page)
        if page >= last_page or not data:
            break
        page += 1
    return matches


def _response_download_url(body: object) -> str | None:
    if isinstance(body, str):
        value = body.strip()
        return value if value.startswith(("https://", "http://")) else None
    if not isinstance(body, dict):
        return None

    keys = ("download_url", "torrent_download_url", "torrent_url", "download", "url", "link")
    candidates = [body]
    for key in ("data", "torrent", "result"):
        value = body.get(key)
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, str):
            candidates.append({"url": value})
    for item in candidates:
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip().startswith(("https://", "http://")):
                return value.strip()
    return None


def _download_with_curl(url: str, torrent_path: Path) -> bool:
    print(f"No download client configured; downloading torrent to {torrent_path}")
    try:
        result = subprocess.run(
            ["curl", "--fail", "--location", "--silent", "--show-error",
             "--retry", "3", "--output", str(torrent_path), url],
            capture_output=True, text=True, check=False, timeout=300,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"Torrent download failed: {short_msg(e) or type(e).__name__}")
        return False
    if result.returncode != 0:
        print(f"Torrent download failed: {short_msg(result.stderr) or f'curl exited {result.returncode}'}")
        return False
    if not torrent_path.is_file() or torrent_path.stat().st_size == 0:
        print("Torrent download failed: the downloaded file is empty or missing.")
        return False
    print(f"Torrent downloaded: {torrent_path}")
    return True


def _send_to_download_client(url: str, client: dict) -> bool:
    kind = str(client.get("type", "")).strip().lower()
    base_url = str(client.get("url", "")).strip().rstrip("/")
    username = str(client.get("username", ""))
    password = str(client.get("password", ""))
    if not base_url or kind not in ("qbittorrent", "transmission"):
        print("Download client configuration must include a supported type (qbittorrent or transmission) and URL.")
        return False

    try:
        session = requests.Session()
        if kind == "qbittorrent":
            login = session.post(
                f"{base_url}/api/v2/auth/login",
                data={"username": username, "password": password}, timeout=30,
            )
            login.raise_for_status()
            if login.text.strip().lower() != "ok.":
                print(f"qBittorrent login failed: {short_msg(login.text)}")
                return False
            data = {"urls": url}
            for key in ("category", "tags", "save_path"):
                if client.get(key):
                    data[key] = client[key]
            response = session.post(f"{base_url}/api/v2/torrents/add", data=data, timeout=60)
            response.raise_for_status()
            if response.text.strip().lower() not in ("", "ok."):
                print(f"qBittorrent rejected the torrent: {short_msg(response.text)}")
                return False
        else:
            session.auth = (username, password) if username or password else None
            endpoint = base_url
            headers = {}
            payload = {"method": "torrent-add", "arguments": {"filename": url}}
            download_dir = client.get("download_dir")
            if download_dir:
                payload["arguments"]["download-dir"] = download_dir
            response = session.post(endpoint, json=payload, headers=headers, timeout=60)
            if response.status_code == 409:
                session_id = response.headers.get("X-Transmission-Session-Id")
                if not session_id:
                    print("Transmission did not provide its session ID.")
                    return False
                response = session.post(endpoint, json=payload,
                                        headers={"X-Transmission-Session-Id": session_id}, timeout=60)
            response.raise_for_status()
            result = response.json()
            if result.get("result") != "success":
                print(f"Transmission rejected the torrent: {result.get('result', 'unknown error')}")
                return False
            added = result.get("arguments", {}).get("torrent-added")
            duplicate = result.get("arguments", {}).get("torrent-duplicate")
            if not added and not duplicate:
                print("Transmission did not confirm that the torrent was added.")
                return False
    except (requests.RequestException, ValueError) as e:
        print(f"Download client request failed: {short_msg(e) or type(e).__name__}")
        return False

    print(f"Torrent successfully sent to {kind}.")
    return True


def deliver_torrent(url: str | None, torrent_path: Path) -> bool:
    if not url:
        print("Tracker upload succeeded, but its response did not contain a direct torrent download URL.")
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        print("Tracker returned an invalid torrent download URL.")
        return False

    client = config.get("download_client")
    if not isinstance(client, dict) or not client:
        return _download_with_curl(url, torrent_path)
    return _send_to_download_client(url, client)


def post_upload(fields: dict, torrent_path: Path, nfo_path: Path) -> tuple[bool, str | None]:
    headers = {"Authorization": f"Bearer {TRACKER_API_TOKEN.strip()}", "Accept": "application/json"}
    try:
        with torrent_path.open("rb") as tf, nfo_path.open("rb") as nf:
            r = requests.post(
                UPLOAD_ENDPOINT, headers=headers, data=tracker_payload(fields),
                files={"torrent": (torrent_path.name, tf, "application/x-bittorrent"),
                       "nfo": (nfo_path.name, nf, "text/plain")},
                timeout=300,
            )
    except (requests.RequestException, OSError) as e:
        print(f"Upload failed: {short_msg(e) or type(e).__name__}")
        return False, None

    raw_text = r.text.strip()
    try:
        body = r.json()
    except ValueError:
        body = raw_text

    success = r.ok
    if isinstance(body, dict):
        success = bool(body.get("success", r.ok))
    elif isinstance(body, str) and any(err in body.lower() for err in ("error", "fail", "unauthorized", "forbidden")):
        success = False

    if not success:
        print(f"Upload failed (HTTP {r.status_code})")
        print(f"Response: {body if body else raw_text}")
        return False, None

    print("Upload successful.")
    download_url = _response_download_url(body)
    if download_url:
        print(f"Torrent download URL: {download_url}")
    elif isinstance(body, dict):
        print(f"Response data: {body}")
    elif isinstance(body, str) and body:
        print(f"Response: {body}")
    return True, download_url

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
    torrent.generate()
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
        if problems := plan_problems(plan):
            print("Cannot upload: " + "; ".join(problems))
            return False
        uploaded, download_url = post_upload(plan.fields, job.torrent_path, job.nfo_path)
        return deliver_torrent(download_url, job.torrent_path) if uploaded else False

    # The web UI (stdin is a pipe) can send "u" followed by one line of JSON edits: apply them,
    # then upload. Invalid edits change nothing and the prompt is shown again.
    from_web = not sys.stdin.isatty()
    while True:
        print_plan(plan)
        print("\n  1) Upload now\n  0) Cancel")
        key = read_key("01u" if from_web else "01")
        if key == "u":
            errors = apply_edits(plan, sys.stdin.readline())
            if errors:
                print("Invalid edit: " + "; ".join(errors))
                continue
            key = "1"
        if key == "1":
            if problems := plan_problems(plan):
                print("Cannot upload: " + "; ".join(problems))
                continue
            uploaded, download_url = post_upload(plan.fields, job.torrent_path, job.nfo_path)
            if uploaded:
                return deliver_torrent(download_url, job.torrent_path)
        elif key == "0":
            return False
