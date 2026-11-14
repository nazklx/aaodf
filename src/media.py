"""Non-video media: detection, tag reading, release-name parsing, naming and NFO text.

Music goes through MusicBrainz and books / audiobooks through Open Library (see
musicbrainz.py / openlibrary.py). Video keeps using the TMDB path in tmdb.py."""
from __future__ import annotations
import json
import re
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import config
from .core import VIDEO_EXTS, find_videos, episode_videos, human_size
from .models import Job, MediaMeta
from .openlibrary import clean_isbn

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".opus", ".wav", ".wv", ".ape",
              ".alac", ".aiff", ".aif", ".wma", ".mka", ".dsf", ".dff"}
BOOK_EXTS = {".epub", ".pdf", ".mobi", ".azw", ".azw3", ".djvu", ".fb2"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | BOOK_EXTS
KINDS = ("video", "music", "audiobook", "book")
KIND_LABELS = {"video": "Video", "music": "Music", "audiobook": "Audiobook", "book": "Book"}
SOURCE_OF_KIND = {"music": "musicbrainz", "audiobook": "openlibrary", "book": "openlibrary"}
SOURCE_LABELS = {"musicbrainz": "MusicBrainz", "openlibrary": "Open Library"}

_AUDIOBOOK_RE = re.compile(r"audio[ ._-]?book|unabridged|abridged|narrated|read[ ._-]by", re.I)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_ISBN_RE = re.compile(r"(?<![0-9])(97[89][0-9-]{10,14})(?![0-9])")
_BRACKETS_RE = re.compile(r"[\[{(][^\]})]*[\]})]")
_FORMAT_WORDS_RE = re.compile(
    r"\b(?:FLAC|MP3|AAC|M4B|M4A|OGG|OPUS|WAV|ALAC|CD|WEB|VINYL|LP|SACD|24[ -]?bit|16[ -]?bit|"
    r"320|V0|V2|CBR|VBR|EPUB|PDF|MOBI|AZW3?|EBOOK|AUDIOBOOK|UNABRIDGED|ABRIDGED|RETAIL)\b", re.I)

def natural_key(path: Path) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(path))]

def file_class(path: Path | str) -> str | None:
    """"video", "audio" or "book" for a file name, None for anything else."""
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in BOOK_EXTS:
        return "book"
    return None

def _walk(target: Path, exts: set[str]) -> list[Path]:
    if target.is_file():
        return [target] if target.suffix.lower() in exts else []
    return sorted((p for p in target.rglob("*")
                   if p.is_file() and p.suffix.lower() in exts
                   and not any(part.startswith(".") for part in p.relative_to(target).parts)),
                  key=natural_key)

def normalize_hint(hint: object) -> str:
    value = str(hint or "auto").strip().lower()
    return value if value in ("auto",) + KINDS else "auto"

def detect_kind(target: Path, hint: object = "auto") -> str | None:
    """Which kind of release `target` (file or folder) is, or None when it holds nothing supported.

    `hint` forces a kind (video, music, audiobook, book); "auto" guesses from the files."""
    hint = normalize_hint(hint)
    if hint == "video":
        return "video" if find_videos(target) else None
    if hint in ("music", "audiobook"):
        return hint if _walk(target, AUDIO_EXTS) else None
    if hint == "book":
        return "book" if _walk(target, BOOK_EXTS) else None
    if find_videos(target):
        return "video"
    audio = _walk(target, AUDIO_EXTS)
    if audio:
        if any(p.suffix.lower() == ".m4b" for p in audio) or _AUDIOBOOK_RE.search(target.name):
            return "audiobook"
        return "music"
    return "book" if _walk(target, BOOK_EXTS) else None

def media_sources(target: Path, kind: str) -> list[Path]:
    """The files of a release, in the order they should be listed (first = main file)."""
    if kind == "video":
        return episode_videos(find_videos(target))
    if kind == "book":
        return sorted(_walk(target, BOOK_EXTS), key=lambda p: (-p.stat().st_size, natural_key(p)))
    return _walk(target, AUDIO_EXTS)

# ---------------------------------------------------------------- tags

def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())

def _number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

def probe_audio(path: Path) -> dict:
    """ffprobe summary of one audio file: {"tags", "codec", "bit_rate", "sample_rate", "bits", "duration"}."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, timeout=120).stdout
        data = json.loads(out)
    except (subprocess.SubprocessError, OSError, ValueError):
        return {}
    fmt = data.get("format") or {}
    stream = next((s for s in data.get("streams") or [] if s.get("codec_type") == "audio"), {})
    tags: dict[str, str] = {}
    for source in (fmt.get("tags") or {}, stream.get("tags") or {}):
        for key, value in source.items():
            tags.setdefault(_norm_key(str(key)), str(value).strip())
    return {
        "tags": tags,
        "codec": str(stream.get("codec_name") or ""),
        "bit_rate": _number(stream.get("bit_rate") or fmt.get("bit_rate")),
        "sample_rate": int(_number(stream.get("sample_rate"))),
        "bits": int(_number(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample"))),
        "duration": _number(fmt.get("duration")),
    }

_CODEC_NAMES = {"flac": "FLAC", "mp3": "MP3", "aac": "AAC", "alac": "ALAC", "opus": "Opus",
                "vorbis": "Ogg Vorbis", "wmav2": "WMA", "wmapro": "WMA", "ape": "APE",
                "wavpack": "WavPack", "dsd_lsbf": "DSD", "dsd_msbf": "DSD"}
_LOSSLESS = {"FLAC", "ALAC", "WAV", "APE", "WavPack"}

def audio_format_label(info: dict, path: Path) -> str:
    """"FLAC", "FLAC 24-96", "MP3 320", "AAC 128", "M4B 64"..."""
    codec = info.get("codec", "")
    name = "WAV" if codec.startswith("pcm_") else _CODEC_NAMES.get(codec) or (path.suffix.lstrip(".").upper() or "Audio")
    if path.suffix.lower() == ".m4b":
        name = "M4B"
    bits, rate = info.get("bits", 0), info.get("sample_rate", 0)
    if name in _LOSSLESS or name == "DSD":
        if bits > 16 or rate > 48000:
            return f"{name} {bits or 24}-{rate / 1000:g}" if rate else name
        return name
    kbps = round(info.get("bit_rate", 0) / 1000)
    if name == "MP3" and kbps >= 310:
        kbps = 320
    return f"{name} {kbps}" if kbps else name

def parse_year(value: object) -> int | None:
    m = _YEAR_RE.search(str(value or ""))
    return int(m.group(1)) if m else None

def epub_meta(path: Path) -> dict:
    """Title / author / ISBN / year from an EPUB's OPF file ({} when it can't be read)."""
    dc = "{http://purl.org/dc/elements/1.1/}"
    try:
        with zipfile.ZipFile(path) as z:
            def read(name: str) -> bytes:
                if z.getinfo(name).file_size > 5_000_000:
                    raise ValueError("too large")
                return z.read(name)
            rootfile = ET.fromstring(read("META-INF/container.xml")).find(".//{*}rootfile")
            opf = ET.fromstring(read(rootfile.get("full-path")))
    except (OSError, KeyError, ValueError, AttributeError, zipfile.BadZipFile, ET.ParseError):
        return {}

    def texts(tag: str) -> list[str]:
        return [(e.text or "").strip() for e in opf.iter(dc + tag) if (e.text or "").strip()]

    out: dict = {}
    if texts("title"):
        out["title"] = texts("title")[0]
    if texts("creator"):
        out["artist"] = ", ".join(texts("creator")[:3])
    for ident in texts("identifier"):
        if re.fullmatch(r"(?i)(?:urn:isbn:|isbn[: ]*)?[0-9X -]{10,20}", ident) and (isbn := clean_isbn(ident)):
            out["isbn"] = isbn
            break
    if year := parse_year(" ".join(texts("date"))):
        out["year"] = year
    return out

def media_tags(kind: str, sources: list[Path]) -> dict:
    """Everything later steps need from the files: format label, track count and embedded tags."""
    tags: dict = {"format": "", "tracks": len(sources), "artist": "", "albumartist": "", "album": "",
                  "title": "", "year": None, "mbid": "", "isbn": "", "narrator": ""}
    if not sources:
        return tags
    if kind in ("music", "audiobook"):
        info = probe_audio(sources[0])
        t = info.get("tags", {})
        tags["format"] = audio_format_label(info, sources[0])
        tags.update(
            artist=t.get("artist", ""), albumartist=t.get("albumartist", ""), album=t.get("album", ""),
            title=t.get("title", ""), mbid=t.get("musicbrainzalbumid", ""), isbn=t.get("isbn", ""),
            narrator=t.get("narrator") or t.get("performer", ""),
            year=parse_year(t.get("originaldate") or t.get("date") or t.get("year")))
    else:
        tags["format"] = "+".join(sorted({p.suffix.lstrip(".").upper() for p in sources}))
        epub = next((p for p in sources if p.suffix.lower() == ".epub"), None)
        if epub:
            tags.update(epub_meta(epub))
    return tags

# ---------------------------------------------------------------- release names

def _split_release(name: str) -> tuple[str, int | None]:
    """Clean a release folder / file name and pull out a year: "Artist - Album (2020) [FLAC]" ->
    ("Artist - Album", 2020)."""
    year = None
    for m in _BRACKETS_RE.finditer(name):
        if y := _YEAR_RE.search(m.group()):
            year = int(y.group(1))
    text = _BRACKETS_RE.sub(" ", name).replace("_", " ")
    if " " not in text.strip():
        text = text.replace(".", " ")
    # A bare trailing year ("Artist - Album 2020") is the year, but not when it follows the " - "
    # separator ("Taylor Swift - 1989" is an album called 1989).
    if year is None and (m := re.search(r"\s+((?:19|20)\d{2})\s*$", text)) and text[:m.start()].strip() and not text[:m.start()].rstrip().endswith("-"):
        year, text = int(m.group(1)), text[:m.start()]
    text = _FORMAT_WORDS_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" -."), year

def parse_music_release(name: str) -> tuple[str | None, str, int | None]:
    """(artist, album, year) from "Artist - Album (Year) [FLAC]"."""
    text, year = _split_release(name)
    parts = re.split(r"\s+-\s+", text, maxsplit=1)
    return (parts[0].strip(), parts[1].strip(), year) if len(parts) == 2 else (None, text, year)

def parse_book_release(name: str, order: str | None = None) -> tuple[str | None, str, int | None]:
    """(author, title, year). The name order comes from the `book_name_order` setting."""
    order = order or str(config.get("book_name_order", "author_title"))
    text, year = _split_release(name)
    parts = re.split(r"\s+-\s+", text, maxsplit=1)
    if len(parts) == 2:
        first, second = parts[0].strip(), parts[1].strip()
        return (second, first, year) if order == "title_author" else (first, second, year)
    if m := re.match(r"(.+?)\s+by\s+(.+)$", text, re.I):
        return m.group(2).strip(), m.group(1).strip(), year
    return None, text, year

def find_isbn(*texts: object) -> str | None:
    for text in texts:
        if clean := clean_isbn(text):
            return clean
        if m := _ISBN_RE.search(str(text or "")):
            if clean := clean_isbn(m.group(1)):
                return clean
    return None

# ---------------------------------------------------------------- metadata lookup

def lookup_media(job: Job) -> MediaMeta | None:
    """MusicBrainz for music, Open Library for books and audiobooks. None = no match / disabled."""
    tags = job.tags or {}
    if job.media_kind == "music":
        if not config.get("musicbrainz_enabled", True):
            print("MusicBrainz lookup is disabled in config - skipping.")
            return None
        from .musicbrainz import lookup_music
        p_artist, p_album, p_year = parse_music_release(job.release)
        return lookup_music(
            mbid=tags.get("mbid"),
            artist=tags.get("albumartist") or tags.get("artist") or p_artist,
            album=tags.get("album") or p_album,
            year=tags.get("year") or p_year,
            tracks=len(job.sources))

    if not config.get("openlibrary_enabled", True):
        print("Open Library lookup is disabled in config - skipping.")
        return None
    from .openlibrary import lookup_book
    order = str(config.get("book_name_order", "author_title"))
    swapped = "title_author" if order == "author_title" else "author_title"
    candidates: list[tuple[str, str | None, int | None]] = []
    tag_title = tags.get("album") or tags.get("title")
    tag_author = tags.get("albumartist") or tags.get("artist") or None
    if tag_title:
        candidates.append((tag_title, tag_author, tags.get("year")))
    for guess in (order, swapped):
        author, title, year = parse_book_release(job.release, guess)
        if title and author:
            candidates.append((title, author, year))
        elif title and guess == order:
            candidates.append((title, None, year))
    candidates = list(dict.fromkeys(candidates))
    return lookup_book(job.media_kind, candidates, find_isbn(tags.get("isbn"), job.release))

def meta_description(meta: MediaMeta) -> str:
    """Tracker description text for a match: cover on top (optional), then the details + source link."""
    cover = meta.extra.get("cover_url")
    if cover and config.get("include_cover_in_description", True):
        return f"[img]{cover}[/img]\n\n{meta.overview}"
    return meta.overview

# ---------------------------------------------------------------- names and NFO

def build_media_name(job: Job) -> str:
    """"Artist - Album (2020) [FLAC]". Without a metadata match the folder / file name is kept."""
    meta, fmt = job.meta, (job.tags or {}).get("format", "")
    if meta is None:
        return job.release
    name = f"{meta.creators} - {meta.title}" if meta.creators else meta.title
    if meta.year:
        name += f" ({meta.year})"
    if fmt:
        name += f" [{fmt}]"
    return name

def _clock(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"

MAX_NFO_FILES = 500

def build_nfo(job: Job) -> str:
    """Plain-text NFO: the release, the source link and a file list (with durations for audio)."""
    tags, meta = job.tags or {}, job.meta
    audio = job.media_kind in ("music", "audiobook")
    shown = job.sources[:MAX_NFO_FILES]
    if audio:
        with ThreadPoolExecutor(max_workers=4) as pool:
            durations = [i.get("duration", 0.0) for i in pool.map(probe_audio, shown)]
    else:
        durations = [0.0] * len(shown)

    root = job.target if job.target.is_dir() else job.target.parent
    lines = [job.name, "=" * len(job.name), ""]
    if meta:
        lines += [meta.label, f"{meta.source_name}: {meta.url}", ""]
    lines.append(f"Type:    {KIND_LABELS.get(job.media_kind, job.media_kind)}")
    if tags.get("format"):
        lines.append(f"Format:  {tags['format']}")
    if tags.get("narrator"):
        lines.append(f"Narrator: {tags['narrator']}")
    lines.append(f"Files:   {len(job.sources)}")
    if audio and any(durations) and len(shown) == len(job.sources):
        lines.append(f"Runtime: {_clock(sum(durations))}")
    try:
        lines.append(f"Size:    {human_size(sum(p.stat().st_size for p in job.sources))}")
    except OSError:
        pass
    lines.append("")
    for path, seconds in zip(shown, durations):
        try:
            rel, size = path.relative_to(root), human_size(path.stat().st_size)
        except (ValueError, OSError):
            rel, size = path.name, ""
        lines.append(f"  {rel}  [{size}{', ' + _clock(seconds) if seconds else ''}]")
    if len(job.sources) > len(shown):
        lines.append(f"  ... and {len(job.sources) - len(shown)} more")
    return "\n".join(lines) + "\n"
