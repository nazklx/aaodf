"""Small shared HTTP helper for the metadata APIs (MusicBrainz, Open Library).

Identifies the client with the configured User-Agent, spaces requests out per host and
retries on 429 / 5xx / connection errors."""
from __future__ import annotations
import threading
import time
import requests
from .config import config

DEFAULT_USER_AGENT = "aaodf/1.0 (+https://github.com/nazklx/aaodf)"
MAX_ATTEMPTS = 4

class MetadataNotFound(Exception):
    """The API answered 404 for this id."""

_lock = threading.Lock()
_next_at: dict[str, float] = {}

def user_agent() -> str:
    return str(config.get("metadata_user_agent") or "").strip() or DEFAULT_USER_AGENT

def timeout() -> float:
    try:
        value = float(config.get("metadata_timeout_seconds", 30))
    except (TypeError, ValueError):
        value = 30.0
    return min(max(value, 1.0), 300.0)

def interval(key: str, default: float, floor: float = 0.0) -> float:
    try:
        value = float(config.get(key, default))
        if value != value:
            raise ValueError
    except (TypeError, ValueError):
        value = default
    return min(max(value, floor), 60.0)

def _wait_for_slot(host: str, gap: float) -> None:
    with _lock:
        now = time.monotonic()
        start = max(now, _next_at.get(host, 0.0))
        _next_at[host] = start + gap
    if start > now:
        time.sleep(start - now)

def _push_back(host: str, seconds: float) -> None:
    with _lock:
        _next_at[host] = max(_next_at.get(host, 0.0), time.monotonic() + seconds)

def _retry_after(response: requests.Response) -> float | None:
    try:
        return min(max(float(response.headers.get("Retry-After", "")), 1.0), 60.0)
    except (TypeError, ValueError):
        return None

def get_json(url: str, params: dict | None = None, *, host: str, gap: float):
    """GET `url` and return the decoded JSON. `gap` is the minimum number of seconds between
    requests to `host` (shared by every caller in this process)."""
    headers = {"User-Agent": user_agent(), "Accept": "application/json"}
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        _wait_for_slot(host, gap)
        delay = min(2.0 * 2 ** attempt, 30.0)
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout())
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
        else:
            if response.status_code == 404:
                raise MetadataNotFound(url)
            if response.status_code == 429 or response.status_code >= 500:
                last = requests.HTTPError(f"HTTP {response.status_code} from {host}", response=response)
                delay = _retry_after(response) or delay
            else:
                response.raise_for_status()
                return response.json()
        if attempt + 1 < MAX_ATTEMPTS:
            _push_back(host, delay)
    raise last if last else RuntimeError(f"Request to {host} failed")
