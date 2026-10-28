from __future__ import annotations
import json
import subprocess
import re
from pathlib import Path
from .config import GROUP_SEPARATOR
from .core import episode_info

_L = r"(?<![A-Za-z0-9])"
_R = r"(?![A-Za-z0-9])"
_RESOLUTION_RE = re.compile(_L + r"(4320p|2160p|1080[pi]|720p|576[pi]|480[pi])" + _R, re.I)
_4K_RE = re.compile(_L + r"4K" + _R, re.I)
_REMUX_RE = re.compile(_L + r"remux" + _R, re.I)
_SOURCES = [
    (re.compile(_L + r"web[ ._-]?dl" + _R, re.I), "WEB-DL"),
    (re.compile(_L + r"web[ ._-]?rip" + _R, re.I), "WEBRip"),
    (re.compile(_L + r"blu[ ._-]?ray" + _R, re.I), "BluRay"),
    (re.compile(_L + r"bd[ ._-]?rip" + _R, re.I), "BDRip"),
    (re.compile(_L + r"br[ ._-]?rip" + _R, re.I), "BRRip"),
    (re.compile(_L + r"hdtv" + _R, re.I), "HDTV"),
    (re.compile(_L + r"dvd[ ._-]?rip" + _R, re.I), "DVDRip"),
    (re.compile(_L + r"hd[ ._-]?rip" + _R, re.I), "HDRip"),
    (re.compile(_L + r"dvd" + _R, re.I), "DVD"),
    (re.compile(_L + r"web" + _R, re.I), "WEB-DL"),
]
_AUDIO_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<codec>TrueHD|DTS[ ._-]?HD[ ._-]?MA|DTS[ ._-]?HD[ ._-]?HRA|DTS[ ._-]?HD|"
    r"DTS[ ._-]?X|DTS[ ._-]?ES|DTS|E[ ._-]?AC[ ._-]?3|DD\+|DDP|DD|AC[ ._-]?3|"
    r"AAC|FLAC|LPCM|PCM|Opus|MP3)"
    r"(?:[ ._-]?(?P<ch>[1-9][ .][0-9]))?"
    r"(?:[ ._-]?(?P<atmos>Atmos))?"
    r"(?:[ ._-]?(?P<ch2>[1-9][ .][0-9]))?"
    r"(?![A-Za-z0-9])",
    re.I,
)
_AUDIO_CANON = {
    "TRUEHD": "TrueHD", "DTSHDMA": "DTS-HD MA", "DTSHDHRA": "DTS-HD HRA",
    "DTSHD": "DTS-HD", "DTSX": "DTS-X", "DTSES": "DTS-ES", "DTS": "DTS",
    "EAC3": "DDP", "DD+": "DDP", "DDP": "DDP", "DD": "DD", "AC3": "DD",
    "AAC": "AAC", "FLAC": "FLAC", "LPCM": "LPCM", "PCM": "LPCM",
    "OPUS": "Opus", "MP3": "MP3",
}
_VIDEO_RE = re.compile(
    _L + r"(H[ ._-]?26[45]|x26[45]|HEVC|AVC|AV1|VC[ ._-]?1|MPEG[ ._-]?2|VP9|XviD|DivX)" + _R,
    re.I,
)
_VIDEO_CANON = {
    "h265": "H.265", "h264": "H.264", "x265": "x265", "x264": "x264",
    "hevc": "HEVC", "avc": "AVC", "av1": "AV1", "vc1": "VC-1",
    "mpeg2": "MPEG-2", "vp9": "VP9", "xvid": "XviD", "divx": "DivX",
}
_GROUP_RE = re.compile(r"(?<!WEB)(?<!Blu)(?<!BD)-([A-Za-z0-9]+)$", re.I)
_SEASON_RE = re.compile(r"\bS\d{1,2}(?:E\d{1,3})?\b|\bSeason \d+\b|\bComplete Series\b", re.I)
_STOP_RE = re.compile(r"\b(?:480|576|720|1080|2160)[pi]\b|\b4K\b|\bUHD\b|\b(?:BluRay|Blu-Ray|BDRip|BRRip|WEB-?DL|WEB-?Rip|HDTV|REMUX|DVDRip|HDRip)\b", re.I)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

def _format_audio(m: re.Match) -> str:
    codec = _AUDIO_CANON[re.sub(r"[ ._-]", "", m.group("codec")).upper()]
    parts = [codec]
    if channels := (m.group("ch") or m.group("ch2")):
        parts.append(channels.replace(" ", "."))
    if m.group("atmos"):
        parts.append("Atmos")
    return " ".join(parts)

_EDITIONS = [
    (re.compile(r"Director'?s?[ ._-]?Cut", re.I), "Director's Cut"),
    (re.compile(r"Final[ ._-]?Cut", re.I), "Final Cut"),
    (re.compile(r"Extended", re.I), "Extended"),
    (re.compile(r"Unrated", re.I), "Unrated"),
    (re.compile(r"Uncut", re.I), "Uncut"),
    (re.compile(r"Theatrical", re.I), "Theatrical"),
    (re.compile(r"Remaster(?:ed)?", re.I), "Remastered"),
    (re.compile(r"Restor(?:ed|ation)", re.I), "Restored"),
    (re.compile(r"IMAX", re.I), "IMAX"),
    (re.compile(r"Criterion", re.I), "Criterion"),
    (re.compile(r"Special[ ._-]?Edition", re.I), "Special"),
    (re.compile(r"Ultimate", re.I), "Ultimate"),
    (re.compile(r"Anniversary", re.I), "Anniversary"),
    (re.compile(r"Collector'?s?", re.I), "Collector's"),
    (re.compile(r"Open[ ._-]?Matte", re.I), "Open Matte"),
    (re.compile(r"Redux", re.I), "Redux"),
]
_EDITION_FILLER = {"edition", "version", "collection", "the", "of", "and", "cut"}
_EDITION_TAG_RE = re.compile(r"\{edition-([^}]*)\}", re.I)
MAX_EDITION_LEN = 24

def simplify_edition(text: str) -> str | None:
    found = []
    for rx, label in _EDITIONS:
        if m := rx.search(text):
            found.append((m.start(), label))
    if found:
        seen = set()
        labels = []
        for _, label in sorted(found):
            if label not in seen:
                seen.add(label)
                labels.append(label)
        return " ".join(labels[:2])
    words = [w for w in re.split(r"[ ._-]+", text) if w and w.lower() not in _EDITION_FILLER]
    short = ""
    for w in words[:3]:
        if len(f"{short} {w}".strip()) > MAX_EDITION_LEN:
            break
        short = f"{short} {w}".strip()
    return short or None

def parse_edition(raw: str) -> str | None:
    if m := _EDITION_TAG_RE.search(raw):
        return simplify_edition(m.group(1))
    text = re.sub(r"[{\[].*?[}\]]", " ", raw)
    cut = len(text)
    if m := _STOP_RE.search(text):
        cut = m.start()
    head = text[:cut]
    start = None
    years = list(_YEAR_RE.finditer(head))
    if years:
        start = years[-1].end()
    if m := _SEASON_RE.search(head):
        start = max(start or 0, m.end())
    if start is None:
        return None
    region = head[start:]
    found = sorted((m.start(), label) for rx, label in _EDITIONS if (m := rx.search(region)))
    seen = set()
    labels = []
    for _, label in found:
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return " ".join(labels[:2]) or None

_HDR_DV_RE = re.compile(_L + r"(?:DV|DoVi|Dolby[ ._-]?Vision)" + _R, re.I)
_HDR_PLUS_RE = re.compile(_L + r"HDR10(?:\+|Plus|P)" + _R.replace("(?!", "(?!\\+|"), re.I)
_HDR10_RE = re.compile(_L + r"HDR10(?![A-Za-z0-9+])", re.I)
_HDR_RE = re.compile(_L + r"HDR(?![A-Za-z0-9])", re.I)
_HLG_RE = re.compile(_L + r"HLG" + _R, re.I)

def parse_hdr(text: str) -> str | None:
    parts = []
    if _HDR_DV_RE.search(text):
        parts.append("DV")
    if _HDR_PLUS_RE.search(text):
        parts.append("HDR10+")
    elif _HDR10_RE.search(text):
        parts.append("HDR10")
    elif _HDR_RE.search(text):
        parts.append("HDR")
    elif _HLG_RE.search(text):
        parts.append("HLG")
    return " ".join(parts) or None

def parse_tags(*names: str) -> dict:
    tags = {"resolution": None, "source": None, "remux": False,
            "audio": None, "video": None, "group": None,
            "edition": None, "hdr": None}
    for raw in names:
        if tags["edition"] is None:
            tags["edition"] = parse_edition(raw)
        text = re.sub(r"[{\[].*?[}\]]", " ", raw)
        res = _RESOLUTION_RE.search(text)
        src = next(((m, label) for rx, label in _SOURCES if (m := rx.search(text))), None)
        if tags["resolution"] is None:
            if res:
                tags["resolution"] = res.group(1).lower()
            elif _4K_RE.search(text):
                tags["resolution"] = "2160p"
        if tags["source"] is None and src:
            tags["source"] = src[1]
        if _REMUX_RE.search(text):
            tags["remux"] = True
        starts = [x.start() for x in (res, src[0] if src else None) if x]
        tail = text[min(starts):] if starts else text
        if tags["audio"] is None and (m := _AUDIO_RE.search(tail)):
            tags["audio"] = _format_audio(m)
        if tags["video"] is None and (m := _VIDEO_RE.search(tail)):
            tags["video"] = _VIDEO_CANON[re.sub(r"[ ._-]", "", m.group(1)).lower()]
        if tags["hdr"] is None:
            tags["hdr"] = parse_hdr(tail)
        if tags["group"] is None and (m := _GROUP_RE.search(raw.strip())):
            tags["group"] = m.group(1)
    return tags

def _resolution_from_dims(width: int, height: int, interlaced: bool) -> str:
    if width >= 7000 or height >= 3600:
        return "4320p"
    if width >= 3200 or height >= 1600:
        return "2160p"
    if width >= 1800 or height >= 900:
        res = 1080
    elif width >= 1200 or height >= 650:
        return "720p"
    elif height >= 500 and (width >= 700 or height >= 560):
        res = 576
    else:
        res = 480
    return f"{res}{'i' if interlaced else 'p'}"

def _audio_from_track(t: dict) -> str | None:
    fmt = str(t.get("Format", ""))
    add = str(t.get("Format_AdditionalFeatures", ""))
    atmos = ("Atmos" in str(t.get("Format_Commercial_IfAny", "")) or "JOC" in add or "16-ch" in add)
    if fmt == "E-AC-3":
        codec = "DDP"
    elif fmt == "AC-3":
        codec = "DD"
    elif fmt.startswith("MLP FBA"):
        codec = "TrueHD"
    elif fmt == "DTS":
        codec = "DTS-X" if "XLL X" in add else "DTS-HD MA" if "XLL" in add else "DTS"
    elif fmt in ("AAC", "FLAC", "Opus"):
        codec = fmt
    elif fmt == "MPEG Audio":
        codec = "MP3"
    elif fmt == "PCM":
        codec = "LPCM"
    else:
        return None
    parts = [codec]
    if m := re.match(r"\d+", str(t.get("Channels", ""))):
        n = int(m.group())
        parts.append(f"{n - 1}.1" if n >= 6 else f"{n}.0")
    if atmos:
        parts.append("Atmos")
    return " ".join(parts)

def mediainfo_tags(video: Path) -> dict:
    try:
        out = subprocess.run(["mediainfo", "--Output=JSON", str(video)],
                             capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout
        tracks = json.loads(out)["media"]["track"]
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        return {}
    vt = next((t for t in tracks if t.get("@type") == "Video"), {})
    at = next((t for t in tracks if t.get("@type") == "Audio"), {})
    found: dict = {}
    try:
        found["resolution"] = _resolution_from_dims(
            int(float(vt["Width"])), int(float(vt["Height"])),
            str(vt.get("ScanType", "")).lower().startswith("interlaced"))
    except (KeyError, ValueError):
        pass
    video_codec = {"HEVC": "H.265", "AVC": "H.264", "AV1": "AV1", "VC-1": "VC-1",
                   "MPEG Video": "MPEG-2", "VP9": "VP9"}.get(str(vt.get("Format")))
    if video_codec:
        found["video"] = video_codec
    if audio := _audio_from_track(at):
        found["audio"] = audio
    hdr_text = " ".join(str(v) for k, v in vt.items() if "hdr" in k.lower() or "transfer" in k.lower())
    hdr = []
    if "dolby vision" in hdr_text.lower():
        hdr.append("DV")
    if "hdr10+" in hdr_text.lower() or "st 2094" in hdr_text.lower():
        hdr.append("HDR10+")
    elif "hdr10" in hdr_text.lower() or "st 2086" in hdr_text.lower():
        hdr.append("HDR10")
    elif "hlg" in hdr_text.lower():
        hdr.append("HLG")
    elif re.search(r"\bPQ\b", hdr_text):
        hdr.append("HDR")
    if hdr:
        found["hdr"] = " ".join(hdr)
    return found

def tv_numbers(sources: list[Path]) -> tuple[int, int, str] | None:
    infos = [i for i in map(episode_info, sources) if i]
    if not infos:
        return None
    if len(sources) == 1:
        s, e = infos[0]
        return s, e, f"S{s:02d}E{e:02d}"
    seasons = sorted({s for s, _ in infos})
    if len(seasons) == 1:
        return seasons[0], 0, f"S{seasons[0]:02d}"
    return 0, 0, f"S{seasons[0]:02d}-S{seasons[-1]:02d}"

def build_name(title: str, year: int | None, season_tag: str | None, tags: dict) -> str:
    source = tags["source"]
    if tags["remux"]:
        source = f"{source} REMUX" if source else "REMUX"
    parts = [title, year, season_tag, tags["edition"], tags["resolution"], source,
             tags["hdr"], tags["audio"], tags["video"]]
    name = " ".join(str(p) for p in parts if p)
    if tags["group"]:
        name += f"{GROUP_SEPARATOR}{tags['group']}"
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return re.sub(r"\s+", " ", name).strip()

def pick_type(tags: dict) -> tuple[str, bool]:
    if tags["remux"]:
        return "remux", True
    known = {"WEB-DL": "webdl", "WEBRip": "webrip", "HDTV": "hdtv",
             "BluRay": "encode", "BDRip": "encode", "BRRip": "encode",
             "DVDRip": "encode", "HDRip": "encode"}
    if tags["source"] in known:
        return known[tags["source"]], True
    return "encode", False
