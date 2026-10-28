from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

import requests


def _mime_type(file_path: Path) -> str:
    return mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"


def upload_imgbox(file_path: Path) -> tuple[str | None, list[str]]:
    """Upload an image to Imgbox and return BBCode plus diagnostic logs."""
    log = [f"Uploading {file_path.name} to imgbox..."]
    try:
        import pyimgbox

        async def _upload():
            async with pyimgbox.Gallery() as gallery:
                return await gallery.upload(str(file_path))

        result = asyncio.run(_upload())
        if result and result.get("success"):
            image_url = result.get("image_url")
            if image_url:
                bbcode = f"[img]{image_url}[/img]"
                log.append(f"imgbox: OK -> {image_url}")
                return bbcode, log
            log.append(f"imgbox: Failed -> Upload succeeded but response had no image_url: {result}")
            return None, log

        err_msg = result.get("error", "Unknown error") if result else "Unknown response structure"
        log.append(f"imgbox: Failed -> {err_msg}")
        return None, log

    except ImportError:
        log.append("imgbox: Error -> 'pyimgbox' is not installed. Run: pip install pyimgbox")
        return None, log
    except Exception as e:
        log.append(f"imgbox: Error -> {str(e)}")
        return None, log


def upload_pixhost(file_path: Path) -> tuple[str | None, list[str]]:
    """Upload an image to Pixhost and return a full-image BBCode link."""
    log = [f"Uploading {file_path.name} to pixhost..."]
    try:
        with file_path.open("rb") as f:
            resp = requests.post(
                "https://api.pixhost.cc/images",
                headers={"Accept": "application/json"},
                files={"img": (file_path.name, f, _mime_type(file_path))},
                data={"content_type": "0", "max_th_size": "420"},
                timeout=120,
            )

        if not resp.ok:
            detail = " ".join(resp.text.split())[:300] or "empty response body"
            log.append(f"pixhost: Failed -> HTTP {resp.status_code}: {detail}")
            return None, log

        try:
            data = resp.json()
        except ValueError:
            detail = " ".join(resp.text.split())[:300] or "empty response body"
            log.append(f"pixhost: Failed -> Invalid JSON response (HTTP {resp.status_code}): {detail}")
            return None, log

        if not isinstance(data, dict):
            log.append(f"pixhost: Failed -> Unexpected API response type: {type(data).__name__}")
            return None, log

        image_url = data.get("show_url") or data.get("image_url") or data.get("url")
        if image_url:
            bbcode = f"[img]{image_url}[/img]"
            log.append(f"pixhost: OK -> {image_url}")
            return bbcode, log

        log.append(f"pixhost: Failed -> No image URL in API response: {data}")
        return None, log

    except requests.RequestException as e:
        log.append(f"pixhost: Error -> {str(e)}")
        return None, log
    except OSError as e:
        log.append(f"pixhost: Error -> {str(e)}")
        return None, log
    except Exception as e:
        log.append(f"pixhost: Error -> {str(e)}")
        return None, log


def upload_catbox(file_path: Path) -> tuple[str | None, list[str]]:
    """Upload an image anonymously to Catbox and return BBCode plus logs."""
    log = [f"Uploading {file_path.name} to catbox..."]
    try:
        headers = {
            "Accept": "*/*",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        with file_path.open("rb") as f:
            resp = requests.post(
                "https://catbox.moe/user/api.php",
                headers=headers,
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (file_path.name, f, _mime_type(file_path))},
                timeout=120,
            )

        response_text = resp.text.strip()
        if resp.status_code == 200 and response_text.startswith(("http://", "https://")):
            bbcode = f"[img]{response_text}[/img]"
            log.append(f"catbox: OK -> {response_text}")
            return bbcode, log

        detail = " ".join(response_text.split())[:300] or "empty response body"
        log.append(f"catbox: Failed -> HTTP {resp.status_code}: {detail}")
        if resp.status_code == 412 and "invalid uploader" in response_text.lower():
            log.append(
                "catbox: Hint -> This response is commonly caused by Catbox rejecting the request's "
                "network/IP or uploader fingerprint. Try without a VPN/proxy, or use another uploader."
            )
        return None, log

    except requests.RequestException as e:
        log.append(f"catbox: Error -> {str(e)}")
        return None, log
    except OSError as e:
        log.append(f"catbox: Error -> {str(e)}")
        return None, log
    except Exception as e:
        log.append(f"catbox: Error -> {str(e)}")
        return None, log


# Imgbox is first as the default; if it fails, the code falls back to Pixhost, then Catbox.
UPLOADERS = [upload_imgbox, upload_pixhost, upload_catbox]


def upload_image(file_path: Path) -> tuple[str | None, list[str]]:
    all_logs: list[str] = []
    for uploader in UPLOADERS:
        bbcode, log = uploader(file_path)
        all_logs.extend(log)
        if bbcode:
            return bbcode, all_logs
    return None, all_logs
