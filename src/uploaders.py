import subprocess
import json
import requests
from pathlib import Path

def upload_imgbox(file_path: Path) -> tuple[str | None, list[str]]:
    """Uploads an image using imgbox-cli and returns specific Imgbox BBCode."""
    log = [f"Uploading {file_path.name} to imgbox..."]
    try:
        result = subprocess.run(
            ["imgbox", "--json", str(file_path)],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            log.append(f"imgbox: Failed -> {err}")
            return None, log

        data = json.loads(result.stdout)
        item = data[0] if isinstance(data, list) and len(data) > 0 else data

        image_url = item.get("image_url")
        thumb_url = item.get("thumbnail_url")

        if image_url and thumb_url:
            # Imgbox specific BBCode: Thumbnail linked to full image
            bbcode = f"[url={image_url}][img]{thumb_url}[/img][/url]"
            log.append(f"imgbox: OK -> {image_url}")
            return bbcode, log
        else:
            log.append(f"imgbox: Failed -> Missing URL fields in CLI output")
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
            
            bbcode = f"[url={show_url}][img]{thumb_url}[/img][/url]"
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
