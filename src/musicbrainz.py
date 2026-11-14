"""MusicBrainz lookup for music uploads.

Looks a release up by the MBID in the files' tags when there is one (Picard writes it), otherwise
searches by artist + album. The release link (https://musicbrainz.org/release/<mbid>) is kept in
MediaMeta.url so it can be given to the tracker."""
from __future__ import annotations
import re
import requests
from .config import config
from .metahttp import MetadataNotFound, get_json, interval
from .models import MediaMeta
from .core import short_msg

_MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
MIN_SEARCH_SCORE = 60
MAX_TRACKLIST_LINES = 250

def site() -> str:
    return str(config.get("musicbrainz_url") or "https://musicbrainz.org").strip().rstrip("/")

def release_url(mbid: str) -> str:
    return f"{site()}/release/{mbid}"

def _get(path: str, params: dict):
    gap = interval("musicbrainz_request_interval", 1.1, floor=1.0)
    return get_json(f"{site()}/ws/2{path}", {**params, "fmt": "json"}, host="musicbrainz", gap=gap)

def _phrase(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

def _credit(credit: object) -> str:
    if not isinstance(credit, list):
        return ""
    return "".join(f"{c.get('name') or (c.get('artist') or {}).get('name', '')}{c.get('joinphrase', '')}"
                   for c in credit if isinstance(c, dict)).strip()

def _year(date: object) -> int | None:
    text = str(date or "")[:4]
    return int(text) if text.isdigit() else None

def _length(ms: object) -> str:
    try:
        seconds = round(int(ms) / 1000)
    except (TypeError, ValueError):
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"

def search_release(artist: str | None, album: str, year: int | None = None,
                   tracks: int | None = None) -> str | None:
    """Best matching release MBID, or None."""
    query = f"release:{_phrase(album)}"
    if artist and artist.casefold() not in ("various artists", "various", "va"):
        query += f" AND artist:{_phrase(artist)}"
    found = _get("/release/", {"query": query, "limit": 10}).get("releases") or []

    def rank(rel: dict) -> int:
        score = int(rel.get("score") or 0)
        if year and str(rel.get("date") or "")[:4] == str(year):
            score += 15
        if tracks and rel.get("track-count") == tracks:
            score += 10
        if str(rel.get("status") or "").lower() == "official":
            score += 3
        return score

    candidates = [r for r in found if isinstance(r, dict) and r.get("id") and int(r.get("score") or 0) >= MIN_SEARCH_SCORE]
    return max(candidates, key=rank)["id"] if candidates else None

def fetch_release(mbid: str) -> dict:
    return _get(f"/release/{mbid}", {"inc": "artist-credits+labels+recordings+release-groups+genres"})

def cover_url(release: dict) -> str | None:
    art = release.get("cover-art-archive")
    if isinstance(art, dict) and art.get("front"):
        base = str(config.get("coverart_url") or "https://coverartarchive.org").strip().rstrip("/")
        return f"{base}/release/{release['id']}/front-500"
    return None

def to_meta(release: dict) -> MediaMeta:
    mbid = str(release["id"])
    group = release.get("release-group") if isinstance(release.get("release-group"), dict) else {}
    creators = _credit(release.get("artist-credit"))
    title = str(release.get("title") or "")
    year = _year(release.get("date")) or _year(group.get("first-release-date"))
    url = release_url(mbid)

    labels = []
    for info in release.get("label-info") or []:
        name = (info.get("label") or {}).get("name") if isinstance(info, dict) else None
        cat = info.get("catalog-number") if isinstance(info, dict) else None
        if name or cat:
            labels.append(" ".join(x for x in (name, f"[{cat}]" if cat else "") if x))
    genres = [g.get("name") for g in (release.get("genres") or group.get("genres") or []) if isinstance(g, dict) and g.get("name")]
    media = [m for m in release.get("media") or [] if isinstance(m, dict)]
    formats = sorted({str(m["format"]) for m in media if m.get("format")})
    track_count = sum(len(m.get("tracks") or []) for m in media)

    lines = [f"{creators} - {title}" + (f" ({year})" if year else "") if creators else title]
    if labels:
        lines.append("Label: " + ", ".join(dict.fromkeys(labels)))
    for key, label in (("country", "Country"), ("barcode", "Barcode"), ("status", "Status")):
        if release.get(key):
            lines.append(f"{label}: {release[key]}")
    if formats:
        lines.append("Format: " + ", ".join(formats))
    if group.get("primary-type"):
        lines.append("Type: " + str(group["primary-type"]))
    if genres:
        lines.append("Genres: " + ", ".join(genres[:8]))

    tracklist: list[str] = []
    for m in media:
        if len(media) > 1:
            tracklist.append(f"Disc {m.get('position', '?')}" + (f": {m['title']}" if m.get("title") else ""))
        for t in m.get("tracks") or []:
            if not isinstance(t, dict):
                continue
            length = _length(t.get("length"))
            tracklist.append(f"{str(t.get('number') or t.get('position') or '?').rjust(2, '0')}. {t.get('title', '')}"
                             + (f" ({length})" if length else ""))
    if tracklist:
        lines += ["", "Tracklist:"] + tracklist[:MAX_TRACKLIST_LINES]
        if len(tracklist) > MAX_TRACKLIST_LINES:
            lines.append("...")
    lines += ["", f"MusicBrainz: {url}"]

    extra = {"track_count": track_count, "formats": formats, "labels": labels, "genres": genres,
             "release_group_id": group.get("id") or "", "cover_url": cover_url(release) or ""}
    return MediaMeta(kind="music", source="musicbrainz", id=mbid, title=title, creators=creators,
                     year=year, overview="\n".join(lines), url=url, extra=extra)

def lookup_music(mbid: str | None, artist: str | None, album: str | None,
                 year: int | None = None, tracks: int | None = None) -> MediaMeta | None:
    try:
        release = None
        if mbid and _MBID_RE.match(mbid.strip()):
            print(f"Looking up MusicBrainz release {mbid.strip()} (from the file tags)...")
            try:
                release = fetch_release(mbid.strip().lower())
            except MetadataNotFound:
                print("That MusicBrainz id does not exist; falling back to a search.")
        if release is None:
            if not album:
                print("No album title found in the tags or folder name - cannot search MusicBrainz.")
                return None
            shown = f"{artist} - {album}" if artist else album
            print(f'Searching MusicBrainz for "{shown}"...')
            found = search_release(artist, album, year, tracks)
            if not found:
                print(f'No MusicBrainz match found for "{shown}".')
                return None
            release = fetch_release(found)
        meta = to_meta(release)
    except (requests.RequestException, ValueError, KeyError, MetadataNotFound) as exc:
        print(f"MusicBrainz lookup failed: {short_msg(exc)}")
        return None
    print(f"Matched on MusicBrainz: {meta.label}")
    return meta
