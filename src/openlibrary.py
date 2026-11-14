"""Open Library lookup for books and audiobooks.

Searches by ISBN when one is known, otherwise by title + author. The work link
(https://openlibrary.org/works/OL...W) is kept in MediaMeta.url so it can be given to the tracker."""
from __future__ import annotations
import re
from difflib import SequenceMatcher
import requests
from .config import config
from .metahttp import MetadataNotFound, get_json, interval
from .models import MediaMeta
from .core import short_msg

SEARCH_FIELDS = ("key,title,author_name,first_publish_year,cover_i,isbn,subject,publisher,"
                 "language,number_of_pages_median,edition_count")
MIN_TITLE_SIMILARITY = 0.6
MAX_DESCRIPTION = 3000

def site() -> str:
    return str(config.get("openlibrary_url") or "https://openlibrary.org").strip().rstrip("/")

def work_url(key: str) -> str:
    return f"{site()}/{key.lstrip('/')}"

def _get(path: str, params: dict | None = None):
    gap = interval("openlibrary_request_interval", 1.0)
    return get_json(f"{site()}{path}", params, host="openlibrary", gap=gap)

def _norm(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()

def _similar(a: object, b: object) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    if a.startswith(b) or b.startswith(a):          # "Dune" vs "Dune: Special Edition"
        ratio = max(ratio, 0.85)
    return ratio

def clean_isbn(raw: object) -> str | None:
    digits = re.sub(r"[^0-9Xx]", "", str(raw or "")).upper()
    if len(digits) == 13 and digits[:3] in ("978", "979") and digits.isdigit():
        return digits
    if len(digits) == 10 and digits[:9].isdigit():
        return digits
    return None

def _search(params: dict) -> list[dict]:
    docs = _get("/search.json", {**params, "limit": 10, "fields": SEARCH_FIELDS}).get("docs") or []
    return [d for d in docs if isinstance(d, dict) and str(d.get("key", "")).startswith("/works/")]

def _pick(docs: list[dict], title: str | None, author: str | None, year: int | None) -> dict | None:
    best, best_score = None, 0.0
    for doc in docs:
        title_sim = _similar(title, doc.get("title"))
        if title and title_sim < MIN_TITLE_SIMILARITY:
            continue
        score = title_sim * 0.7
        names = doc.get("author_name") or []
        if author and names:
            score += max(_similar(author, n) for n in names) * 0.3
        if year and doc.get("first_publish_year") == year:
            score += 0.05
        if score > best_score:
            best, best_score = doc, score
    return best

def _description(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("value")
    text = str(value or "").replace("\r\n", "\n").strip()
    text = re.split(r"\n-{5,}", text)[0].strip()        # drop the "-----  Also contained in" trailers
    return text if len(text) <= MAX_DESCRIPTION else text[:MAX_DESCRIPTION].rstrip() + "..."

def cover_url(doc: dict) -> str | None:
    if doc.get("cover_i"):
        base = str(config.get("openlibrary_covers_url") or "https://covers.openlibrary.org").strip().rstrip("/")
        return f"{base}/b/id/{doc['cover_i']}-L.jpg"
    return None

def to_meta(kind: str, doc: dict, work: dict | None) -> MediaMeta:
    key = str(doc["key"])
    url = work_url(key)
    authors = ", ".join(doc.get("author_name") or [])
    title = str(doc.get("title") or (work or {}).get("title") or "")
    year = doc.get("first_publish_year")
    year = int(year) if isinstance(year, int) or str(year).isdigit() else None
    description = _description((work or {}).get("description"))
    subjects = [str(s) for s in ((work or {}).get("subjects") or doc.get("subject") or [])][:10]
    isbns = [i for i in (doc.get("isbn") or []) if clean_isbn(i)]
    isbn13 = next((i for i in isbns if len(i) == 13), isbns[0] if isbns else "")

    lines = [f"{authors} - {title}" + (f" ({year})" if year else "") if authors else title]
    if doc.get("publisher"):
        lines.append("Publisher: " + ", ".join(dict.fromkeys(str(p) for p in doc["publisher"][:3])))
    if isbn13:
        lines.append(f"ISBN: {isbn13}")
    if doc.get("number_of_pages_median"):
        lines.append(f"Pages: {doc['number_of_pages_median']}")
    if doc.get("language"):
        lines.append("Language: " + ", ".join(str(x) for x in doc["language"][:3]))
    if subjects:
        lines.append("Subjects: " + ", ".join(subjects))
    if description:
        lines += ["", description]
    lines += ["", f"Open Library: {url}"]

    extra = {"isbn": isbn13, "subjects": subjects, "cover_url": cover_url(doc) or "",
             "edition_count": doc.get("edition_count") or 0}
    return MediaMeta(kind=kind, source="openlibrary", id=key.rsplit("/", 1)[-1], title=title,
                     creators=authors, year=year, overview="\n".join(lines), url=url, extra=extra)

def lookup_book(kind: str, candidates: list[tuple[str, str | None, int | None]],
                isbn: str | None = None) -> MediaMeta | None:
    """`candidates` are (title, author, year) guesses tried in order; `isbn` is tried first."""
    try:
        doc, shown = None, ""
        clean = clean_isbn(isbn)
        if clean:
            print(f"Searching Open Library for ISBN {clean}...")
            docs = _search({"isbn": clean})
            doc, shown = (docs[0] if docs else None), f"ISBN {clean}"
        for title, author, year in ([] if doc else candidates):
            shown = f"{author} - {title}" if author else title
            print(f'Searching Open Library for "{shown}"...')
            docs = _search({"title": title, **({"author": author} if author else {})})
            if not docs and author:
                docs = _search({"q": f"{author} {title}"})
            doc = _pick(docs, title, author, year)
            if doc:
                break
        if not doc:
            print(f'No Open Library match found for "{shown or "this release"}".')
            return None
        try:
            work = _get(f"{doc['key']}.json")
        except MetadataNotFound:
            work = None
        except (requests.RequestException, ValueError) as exc:     # the description is optional
            print(f"Could not fetch the Open Library work record: {short_msg(exc)}")
            work = None
        meta = to_meta(kind, doc, work if isinstance(work, dict) else None)
    except (requests.RequestException, ValueError, KeyError, MetadataNotFound) as exc:
        print(f"Open Library lookup failed: {short_msg(exc)}")
        return None
    print(f"Matched on Open Library: {meta.label}")
    return meta
