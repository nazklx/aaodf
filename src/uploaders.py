from __future__ import annotations
import asyncio
import requests
from pathlib import Path

def upload_imgbox(file_path: Path) -> tuple[str | None, list[str]]:
    """Uploads an image using the pyimgbox library and returns specific Imgbox BBCode."""
    log = [f"Uploading {file_path.name} to imgbox..."]
    try:
        import pyimgbox

        async def _upload():
            async with pyimgbox.Gallery() as gallery:
                return await gallery.upload(str(file_path))

        result = asyncio.run(_upload())
        if result and result.get("success"):
            image_url = result.get("image_url")
            thumb_url = result.get("thumbnail_url")
            
            # Imgbox specific BBCode: Thumbnail linked to full image
            bbcode = f"[img]{image_url}[/img]"
            log.append(f"imgbox: OK -> {image_url}")
            return bbcode, log
        else:
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
    """Uploads an image to Pixhost with its own specific formatting."""
    log = [f"Uploading {file_path.name} to pixhost..."]
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://api.pixhost.to/images",
                files={"img": f},
                data={"content_type": "0"},
                timeout=30
            )
        
        try:
            data = resp.json()
        except Exception:
            log.append(f"pixhost: Failed -> Invalid JSON response (HTTP {resp.status_code})")
            return None, log

        if "show_url" in data and "image_url" in data:
            image_url = data["image_url"]
            show_url = data["show_url"]
            thumb_url = data.get("thumb_url", image_url)
            
            bbcode = f"[[img]{show_url}[/img]"
            log.append(f"pixhost: OK -> {image_url}")
            return bbcode, log
        else:
            log.append(f"pixhost: Failed -> Unexpected API response structure")
            return None, log
            
    except Exception as e:
        log.append(f"pixhost: Error -> {str(e)}")
        return None, log


def upload_catbox(file_path: Path) -> tuple[str | None, list[str]]:
    """Uploads an image to Catbox with its own formatting."""
    log = [f"Uploading {file_path.name} to catbox..."]
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": f},
                timeout=30
            )
        
        if resp.status_code == 200 and resp.text.startswith("http"):
            image_url = resp.text.strip()
            bbcode = f"[img]{image_url}[/img]"
            log.append(f"catbox: OK -> {image_url}")
            return bbcode, log
        else:
            log.append(f"catbox: Failed -> HTTP {resp.status_code}: {resp.text.strip()}")
            return None, log
            
    except Exception as e:
        log.append(f"catbox: Error -> {str(e)}")
        return None, log


# Imgbox is first as the default; if it fails, it falls back to Pixhost, then Catbox.
UPLOADERS = [upload_imgbox, upload_pixhost, upload_catbox]

def upload_image(file_path: Path) -> tuple[str | None, list[str]]:
    all_logs = []
    for uploader in UPLOADERS:
        bbcode, log = uploader(file_path)
        all_logs.extend(log)
        if bbcode:
            return bbcode, all_logs
    return None, all_logs
