from __future__ import annotations
import re
import requests
from .config import TMDB_API_KEY
from .models import TmdbInfo
from .core import short_msg

TMDB_API = "https://api.themoviedb.org/3"
_SEASON_RE = re.compile(r"\bS\d{1,2}(?:E\d{1,3})?\b|\bSeason \d+\b|\bComplete Series\b", re.I)
_STOP_RE = re.compile(r"\b(?:480|576|720|1080|2160)[pi]\b|\b4K\b|\bUHD\b|\b(?:BluRay|Blu-Ray|BDRip|BRRip|WEB-?DL|WEB-?Rip|HDTV|REMUX|DVDRip|HDRip)\b", re.I)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

def parse_release(release: str) -> tuple[str, int | None, bool]:
    text = re.sub(r"[{\[].*?[}\]]", " ", release)
    text = re.sub(r"[._()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    cut = len(text)
    is_tv = False
    if m := _SEASON_RE.search(text):
        is_tv = True
        cut = m.start()
    if m := _STOP_RE.search(text):
        cut = min(cut, m.start())
    head = text[:cut].strip()

    year = None
    years = [m for m in _YEAR_RE.finditer(head) if m.start() > 0]
    if years:
        year = int(years[-1].group())
        head = head[: years[-1].start()]
    return head.strip(" -") or text, year, is_tv

def tmdb_get(path: str, params: dict) -> dict:
    key = TMDB_API_KEY.strip()
    headers = {"Accept": "application/json"}
    params = dict(params)
    if key.count(".") == 2:
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    r = requests.get(f"{TMDB_API}{path}", params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()

def tmdb_find(title: str, year: int | None, is_tv: bool) -> TmdbInfo | None:
    for kind in (("tv", "movie") if is_tv else ("movie", "tv")):
        params = {"query": title, "include_adult": "false"}
        if year:
            params["first_air_date_year" if kind == "tv" else "year"] = year
        results = tmdb_get(f"/search/{kind}", params).get("results") or []
        if not results:
            continue
        top = results[0]
        date = (top.get("first_air_date") or top.get("release_date") or "")[:4]
        return TmdbInfo(
            kind=kind,
            id=int(top["id"]),
            title=top.get("name") or top.get("title") or title,
            year=int(date) if date.isdigit() else None,
            overview=(top.get("overview") or "").strip(),
        )
    return None

def tmdb_external_ids(info: TmdbInfo) -> None:
    data = tmdb_get(f"/{info.kind}/{info.id}/external_ids", {})
    imdb = str(data.get("imdb_id") or "")
    info.imdb_id = int(imdb[2:]) if imdb[:2] == "tt" and imdb[2:].isdigit() else 0
    info.tvdb_id = int(data.get("tvdb_id") or 0)

def lookup_tmdb(release: str, tv_hint: bool = False) -> TmdbInfo | None:
    if not TMDB_API_KEY.strip():
        print("TMDB_API_KEY is empty - skipping description lookup.")
        return None
    title, year, is_tv = parse_release(release)
    is_tv = is_tv or tv_hint
    query = f"{title} ({year})" if year else title
    print(f'Searching TMDB for "{query}"...')
    try:
        info = tmdb_find(title, year, is_tv)
    except (requests.RequestException, ValueError) as e:
        print(f"TMDB lookup failed: {short_msg(e)}")
        return None
    if info is None:
        print(f'No TMDB match found for "{query}".')
        return None
    print(f"Matched on TMDB: {info.label}")
    try:
        tmdb_external_ids(info)
    except (requests.RequestException, ValueError) as e:
        print(f"Could not fetch IMDb/TVDB ids: {short_msg(e)}")
    if not info.overview:
        print("TMDB has no description for this title.")
    return info

def tmdb_by_id(kind: str, tmdb_id: int) -> TmdbInfo:
    data = tmdb_get(f"/{kind}/{tmdb_id}", {})
    date = (data.get("first_air_date") or data.get("release_date") or "")[:4]
    info = TmdbInfo(
        kind=kind, id=int(data["id"]),
        title=data.get("name") or data.get("title") or "",
        year=int(date) if date.isdigit() else None,
        overview=(data.get("overview") or "").strip(),
    )
    try:
        tmdb_external_ids(info)
    except (requests.RequestException, ValueError):
        pass
    return info
