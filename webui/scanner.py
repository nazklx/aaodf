"""Background tracker duplicate scan.

Tracker searches are rate limited (see `tracker_search_rate_limit`), so a scan of a big
download directory takes minutes. It therefore runs in a thread and the browser polls
for progress instead of holding one long HTTP request open.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path

from src.config import load_config
from src.media import MEDIA_EXTS, detect_kind, normalize_hint
from src.tracker import SearchCancelled, search_tracker, tracker_search_title

log = logging.getLogger(__name__)

MAX_ENTRIES = 5000


class ScanBusy(Exception):
    pass


class ScanTooLarge(Exception):
    pass


class ScanManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.scan: dict | None = None
        self.cancel_event = threading.Event()

    # ---------- public API ----------
    def start(self, directory: Path) -> dict:
        with self.lock:
            if self.scan and self.scan["status"] == "running":
                raise ScanBusy()
            candidates = [p for p in directory.iterdir() if not p.name.startswith(".") and p.is_dir()]
            candidates += [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS]
            candidates.sort(key=lambda p: (not p.is_dir(), p.name.casefold()))
            if len(candidates) > MAX_ENTRIES:
                raise ScanTooLarge(f"Directory contains more than {MAX_ENTRIES} immediate entries")
            self.cancel_event = threading.Event()
            self.scan = {
                "id": uuid.uuid4().hex[:10], "path": str(directory), "status": "running",
                "total": len(candidates), "done": 0, "current": "", "error": None,
                "started": time.time(), "finished": None, "results": [],
            }
            threading.Thread(target=self._run, args=(self.scan, candidates, self.cancel_event), daemon=True).start()
            return self._snapshot("", 0)

    def snapshot(self, scan_id: str, since: int) -> dict | None:
        with self.lock:
            return self._snapshot(scan_id, since) if self.scan else None

    def cancel(self) -> bool:
        with self.lock:
            if not self.scan or self.scan["status"] != "running":
                return False
            self.cancel_event.set()
            return True

    # ---------- internals ----------
    def _snapshot(self, scan_id: str, since: int) -> dict:
        s = self.scan
        # A different scan id means the client is looking at an older scan: send everything.
        start = max(0, since) if scan_id == s["id"] else 0
        out = {k: v for k, v in s.items() if k != "results"}
        out["results"] = s["results"][start:]
        out["since"] = start
        return out

    @staticmethod
    def _check(candidate: Path, cancel: threading.Event) -> dict:
        is_dir = candidate.is_dir()
        # Uses the configured media_type (auto-detect by default); per-job overrides don't apply to scans.
        media = detect_kind(candidate, normalize_hint(load_config().get("media_type", "auto")))
        query = tracker_search_title(candidate.name, media or "video")
        item = {
            "path": str(candidate), "name": candidate.name, "kind": "folder" if is_dir else "file",
            "media": media, "query": query, "matches": [], "match_count": 0,
        }
        # A folder with no supported media can't become a job, so don't spend a request on it.
        if is_dir and media is None:
            return {**item, "eligible": False, "status": "skipped"}
        matches = search_tracker(query, cancel=cancel)
        return {**item, "matches": matches[:10], "match_count": len(matches), "eligible": True,
                "status": "matched" if matches else "unmatched"}

    def _run(self, scan: dict, candidates: list[Path], cancel: threading.Event) -> None:
        status, error, name = "done", None, ""
        try:
            for candidate in candidates:
                if cancel.is_set():
                    raise SearchCancelled()
                name = candidate.name
                with self.lock:
                    scan["current"] = name
                item = self._check(candidate, cancel)
                with self.lock:
                    scan["results"].append(item)
                    scan["done"] += 1
        except SearchCancelled:
            status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - any failure must end the scan visibly
            log.warning("Duplicate scan failed for %s: %s", name, exc)
            status, error = "failed", f"Tracker search failed for {name}: {exc}"
        with self.lock:
            scan.update(status=status, error=error, finished=time.time(), current="")
