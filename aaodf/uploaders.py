from __future__ import annotations
import json
import os
import shutil
import subprocess
import requests
from pathlib import Path
from .models import UploadError
from .core import short_msg

UPLOAD_TIMEOUT = 120

def upload_imgbox(f: Path) -> str:
    if not shutil.which("imgbox"):
        raise UploadError("imgbox CLI not installed")
    try:
        r = subprocess.run(["imgbox", "--json", str(f)], capture_output=True,
                           text=True, timeout=UPLOAD_TIMEOUT, stdin=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError) as e:
        raise UploadError(short_msg(e) or type(e).__name__)
    if r.returncode != 0:
        raise UploadError(f"exit {r.returncode}: {short_msg(r.stderr)}")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise UploadError("could not parse imgbox output")
    items = data if isinstance(data, list) else (list(data.values()) if isinstance(data, dict) else [])
    for item in items:
        if isinstance(item, dict) and item.get("success") and item.get("image_url"):
            return f"[img]{item['image_url']}[/img]"
    raise UploadError("response reported no successful upload")

def upload_pixhost(f: Path) -> str:
    try:
        with f.open("rb") as fh:
            r = requests.post(
                "https://api.pixhost.cc/images",
                headers={"Accept": "application/json"},
                files={"img": (f.name, fh)},
                data={"content_type": "0", "max_th_size": "0"},
                timeout=UPLOAD_TIMEOUT,
            )
        r.raise_for_status()
        url = r.json().get("show_url")
    except (requests.RequestException, ValueError, OSError) as e:
        raise UploadError(short_msg(e) or type(e).__name__)
    if not url:
        raise UploadError("no show_url in response")
    return f"[img]{url}[/img]"

def upload_catbox(f: Path) -> str:
    data = {"reqtype": "fileupload"}
    if userhash := os.environ.get("CATBOX_USERHASH"):
        data["userhash"] = userhash
    try:
        with f.open("rb") as fh:
            r = requests.post("https://catbox.moe/user/api.php", data=data,
                              files={"fileToUpload": (f.name, fh)}, timeout=UPLOAD_TIMEOUT)
        r.raise_for_status()
    except (requests.RequestException, OSError) as e:
        raise UploadError(short_msg(e) or type(e).__name__)
    text = r.text.strip()
    if not text.startswith("https://"):
        raise UploadError(short_msg(text) or "unexpected response")
    return f"[img]{text}[/img]"

UPLOADERS = [upload_imgbox, upload_pixhost, upload_catbox]

def upload_image(f: Path) -> tuple[str | None, list[str]]:
    log: list[str] = []
    for up in UPLOADERS:
        name = up.__name__.removeprefix("upload_")
        try:
            bb = up(f)
        except UploadError as e:
            log.append(f"    {name}: failed ({e})")
            continue
        log.append(f"    {name}: OK")
        return bb, log
    log.append("    -> all hosts failed")
    return None, log