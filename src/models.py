from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class TmdbInfo:
    kind: str
    id: int
    title: str
    year: int | None
    overview: str
    imdb_id: int = 0
    tvdb_id: int = 0

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title

@dataclass
class MediaMeta:
    """Metadata for music (MusicBrainz) and books / audiobooks (Open Library).

    `url` is the canonical MusicBrainz release / Open Library work link. It is kept all the way
    to the tracker upload (see tracker.compute_media_plan / tracker_payload)."""
    kind: str               # "music" | "audiobook" | "book"
    source: str             # "musicbrainz" | "openlibrary"
    id: str                 # MusicBrainz release MBID or Open Library work key (OL45804W)
    title: str
    creators: str           # artist(s) or author(s)
    year: int | None
    overview: str           # text for the tracker description (includes the source link)
    url: str
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        base = f"{self.creators} - {self.title}" if self.creators else self.title
        return f"{base} ({self.year})" if self.year else base

    @property
    def source_name(self) -> str:
        return {"musicbrainz": "MusicBrainz", "openlibrary": "Open Library"}.get(self.source, self.source)

@dataclass
class Job:
    target: Path
    sources: list[Path]
    first: Path
    release: str
    name: str
    out_dir: Path
    torrent_path: Path
    nfo_path: Path
    tv_hint: bool
    info: TmdbInfo | None = None
    overview: str = ""
    links: list[str] = field(default_factory=list)
    description: str = ""
    tags: dict | None = None
    media_kind: str = "video"       # "video" | "music" | "audiobook" | "book"
    meta: MediaMeta | None = None   # set for every kind except video

@dataclass
class Plan:
    fields: dict
    is_tv: bool
    tv: tuple[int, int, str] | None
    type_key: str
    type_known: bool
    info: TmdbInfo | MediaMeta | None
    missing: list[str]
    edited: set[str] = field(default_factory=set)
    media_kind: str = "video"

class UploadError(Exception):
    pass
