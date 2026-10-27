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

@dataclass
class Plan:
    fields: dict
    is_tv: bool
    tv: tuple[int, int, str] | None
    type_key: str
    type_known: bool
    info: TmdbInfo
    missing: list[str]
    edited: set[str] = field(default_factory=set)

class UploadError(Exception):
    pass