"""Flask web UI for the uploader: file browser, job queue, image library, settings."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from flask import Flask, Response, abort, jsonify, make_response, redirect, request, send_file, send_from_directory

from .jobs import ACTIVE, OUTPUT, ROOT, STATE_DIR, JobManager, inspect_target
from src.core import find_videos
from src.tracker import search_tracker, tracker_search_title

VIDEO_EXTS = {".mkv", ".mp4"}
SENSITIVE = ("announce_url", "tmdb_api_key", "tracker_api_token", "upload_endpoint")
BOOL_KEYS = ("confirm_before_upload", "anonymous_upload", "personal_release", "stream_optimized")
STR_KEYS = ("group_separator", "run_as_user", "tracker_search_endpoint")
NUM_KEYS = ("screenshot_count", "screenshot_start_seconds", "screenshot_interval_seconds")
MAX_ENTRIES = 5000
COOKIE = "aaodf_token"


def create_app(fs_root: Path, token: str, max_concurrent: int = 1) -> Flask:
    app = Flask(__name__, static_folder=str(Path(__file__).parent / "static"), static_url_path="/static")
    manager = JobManager(max_concurrent)
    fs_root = fs_root.resolve()
    OUTPUT.mkdir(exist_ok=True)

    # ---------- auth ----------
    @app.before_request
    def guard():
        if request.endpoint == "static":
            return None
        supplied = request.cookies.get(COOKIE) or request.headers.get("X-Token", "")
        if hmac.compare_digest(supplied, token):
            if request.method not in ("GET", "HEAD") and not request.is_json:
                abort(415)  # forces a CORS preflight, blocks cross-site form posts
            return None
        if request.method == "GET" and request.path == "/" and hmac.compare_digest(request.args.get("token", ""), token):
            resp = make_response(redirect("/"))
            resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict")
            return resp
        if request.path == "/":
            return Response("Open the URL printed in the terminal (it contains ?token=...).", 401)
        return jsonify(error="unauthorized"), 401

    @app.errorhandler(Exception)
    def on_error(e):
        code = getattr(e, "code", 500)
        if code == 500:
            app.logger.exception(e)
        return jsonify(error=getattr(e, "description", str(e))), code if isinstance(code, int) else 500

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    # ---------- filesystem ----------
    def safe_path(raw: str) -> Path:
        p = Path(os.path.realpath(raw or str(fs_root)))
        if p != fs_root and fs_root not in p.parents:
            abort(403, f"Outside the allowed root ({fs_root})")
        return p

    @app.get("/api/fs")
    def fs_list():
        p = safe_path(request.args.get("path", str(fs_root)))
        if not p.is_dir():
            abort(400, "Not a directory")
        show_hidden = request.args.get("hidden") == "1"
        entries, truncated = [], False
        try:
            with os.scandir(p) as it:
                for e in it:
                    if not show_hidden and e.name.startswith("."):
                        continue
                    if len(entries) >= MAX_ENTRIES:
                        truncated = True
                        break
                    try:
                        st = e.stat()
                        is_dir = e.is_dir()
                    except OSError:
                        continue
                    entries.append({
                        "name": e.name, "path": os.path.join(str(p), e.name), "dir": is_dir,
                        "size": None if is_dir else st.st_size, "mtime": st.st_mtime,
                        "video": (not is_dir) and Path(e.name).suffix.lower() in VIDEO_EXTS,
                    })
        except PermissionError:
            abort(403, "Permission denied")
        entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        parent = str(p.parent) if p != fs_root else None
        return jsonify(path=str(p), parent=parent, root=str(fs_root), entries=entries, truncated=truncated)

    @app.get("/api/inspect")
    def inspect():
        return jsonify(inspect_target(safe_path(request.args.get("path", ""))))

    @app.post("/api/duplicates/scan")
    def duplicate_scan():
        body = request.get_json() or {}
        raw_directory = str(body.get("path", "")).strip()
        if not raw_directory:
            abort(400, "Select a download directory")
        directory = safe_path(raw_directory)
        if not directory.is_dir():
            abort(400, "Select a valid download directory")

        candidates = [p for p in directory.iterdir() if not p.name.startswith(".") and p.is_dir()]
        candidates += [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
        candidates.sort(key=lambda p: (not p.is_dir(), p.name.casefold()))
        if len(candidates) > MAX_ENTRIES:
            abort(413, f"Directory contains more than {MAX_ENTRIES} immediate entries")
        results = []
        for candidate in candidates:
            query = tracker_search_title(candidate.name)
            try:
                matches = search_tracker(query)
            except Exception as exc:
                app.logger.warning("Duplicate scan failed for %s: %s", candidate, exc)
                abort(502, f"Tracker search failed for {candidate.name}: {exc}. No batch was created.")

            eligible = not candidate.is_dir() or bool(find_videos(candidate))
            results.append({
                "path": str(candidate),
                "name": candidate.name,
                "kind": "folder" if candidate.is_dir() else "file",
                "query": query,
                "matches": matches[:10],
                "match_count": len(matches),
                "eligible": eligible,
                "status": "matched" if matches else ("unmatched" if eligible else "skipped"),
            })
        return jsonify(path=str(directory), scanned=len(candidates), results=results)

    # ---------- jobs ----------
    @app.get("/api/jobs")
    def jobs_list():
        return jsonify(jobs=manager.list(), max_concurrent=manager.max_concurrent)

    @app.post("/api/jobs")
    def jobs_add():
        body = request.get_json()
        paths = [str(safe_path(p)) for p in body.get("paths", [])]
        if not paths:
            abort(400, "No paths given")
        opts = body.get("options", {})
        clean = {k: bool(opts[k]) for k in ("review", "auto_upload", "anonymous_upload", "personal_release", "stream_optimized") if k in opts}
        created, skipped = manager.add(paths, clean)
        return jsonify(created=created, skipped=skipped)

    @app.get("/api/jobs/<jid>")
    def job_get(jid):
        j = manager.get(jid) or abort(404, "No such job")
        return jsonify(j.public(len(manager.logs.get(jid, []))))

    @app.get("/api/jobs/<jid>/log")
    def job_log(jid):
        if not manager.get(jid):
            abort(404, "No such job")
        since = int(request.args.get("since", 0))
        return jsonify(lines=manager.log_since(jid, since), since=since)

    def action(fn, *a):
        err = fn(*a)
        if err:
            abort(409, err)
        return jsonify(ok=True)

    @app.post("/api/jobs/<jid>/proceed")
    def job_proceed(jid):
        return action(manager.proceed, jid)

    @app.post("/api/jobs/<jid>/confirm")
    def job_confirm(jid):
        return action(manager.confirm, jid, bool(request.get_json().get("go")))

    @app.post("/api/jobs/<jid>/regenerate")
    def job_regen(jid):
        return action(manager.regenerate, jid)

    @app.post("/api/jobs/<jid>/cancel")
    def job_cancel(jid):
        return jsonify(ok=manager.cancel(jid))

    @app.post("/api/jobs/<jid>/retry")
    def job_retry(jid):
        j = manager.retry(jid)
        if not j:
            abort(409, "Cannot retry (still active or already queued)")
        return jsonify(job=j)

    @app.delete("/api/jobs/<jid>")
    def job_delete(jid):
        if not manager.remove(jid):
            abort(409, "Job is still active")
        return jsonify(ok=True)

    @app.post("/api/jobs/clear")
    def jobs_clear():
        return jsonify(removed=manager.clear_finished())

    @app.post("/api/queue")
    def queue_settings():
        manager.set_max_concurrent(int(request.get_json().get("max_concurrent", 1)))
        return jsonify(max_concurrent=manager.max_concurrent)

    # ---------- output library / images ----------
    def out_dir(name: str) -> Path:
        if "/" in name or "\\" in name or not name.startswith("UPLOAD."):
            abort(400, "Bad folder name")
        p = OUTPUT / name
        if not p.is_dir():
            abort(404, "No such output folder")
        return p

    def image_path(name: str, img: str) -> Path:
        if "/" in img or "\\" in img or not img.lower().endswith(".png"):
            abort(400, "Bad image name")
        p = out_dir(name) / img
        if not p.is_file():
            abort(404, "No such image")
        return p

    def job_for(name: str):
        return next((j for j in manager.jobs.values() if j.out_dir == name and j.status in ACTIVE), None)

    @app.get("/api/outputs")
    def outputs_list():
        items = []
        for d in OUTPUT.glob("UPLOAD.*"):
            if not d.is_dir():
                continue
            pngs = list(d.glob("*.png"))
            owner = job_for(d.name)
            items.append({
                "name": d.name, "images": len(pngs), "mtime": d.stat().st_mtime,
                "torrent": any(d.glob("*.torrent")), "job": owner.id if owner else None,
                "cover": sorted(p.name for p in pngs)[0] if pngs else None,
            })
        items.sort(key=lambda x: -x["mtime"])
        return jsonify(outputs=items)

    @app.get("/api/outputs/<name>/images")
    def outputs_images(name):
        d = out_dir(name)
        imgs = [{"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime} for p in sorted(d.glob("*.png"))]
        return jsonify(images=imgs)

    @app.get("/api/outputs/<name>/image/<img>")
    def outputs_image(name, img):
        return send_file(image_path(name, img), mimetype="image/png", max_age=0)

    @app.get("/api/outputs/<name>/thumb/<img>")
    def outputs_thumb(name, img):
        src = image_path(name, img)
        st = src.stat()
        key = hashlib.sha1(f"{src}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()
        thumb = STATE_DIR / "thumbs" / f"{key}.jpg"
        if not thumb.exists():
            thumb.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                                "-vf", "scale=480:-2", "-q:v", "5", "-frames:v", "1", str(thumb)],
                               capture_output=True, stdin=subprocess.DEVNULL)
            if r.returncode != 0 or not thumb.exists():
                return send_file(src, mimetype="image/png")
        return send_file(thumb, mimetype="image/jpeg", max_age=3600)

    @app.delete("/api/outputs/<name>/images")
    def outputs_delete_images(name):
        d = out_dir(name)
        owner = job_for(name)
        if owner and owner.status == "running" and owner.stage not in ("frames",):
            abort(409, "A job is using these images right now")
        if owner and owner.regenerating:
            abort(409, "Frames are being regenerated")
        deleted = []
        for img in request.get_json().get("names", []):
            p = image_path(name, img)
            p.unlink()
            deleted.append(img)
        return jsonify(deleted=deleted, remaining=len(list(d.glob("*.png"))))

    @app.delete("/api/outputs/<name>")
    def outputs_delete(name):
        d = out_dir(name)
        if job_for(name):
            abort(409, "An active job uses this folder")
        shutil.rmtree(d)
        return jsonify(ok=True)

    # ---------- settings ----------
    from src.config import DEFAULT_CONFIG

    def read_cfg() -> dict:
        cfg = dict(DEFAULT_CONFIG)
        try:
            cfg.update(json.loads((ROOT / "config.json").read_text(encoding="utf-8")))
        except Exception:
            pass
        return cfg

    @app.get("/api/config")
    def config_get():
        cfg = read_cfg()
        out = {k: cfg.get(k) for k in BOOL_KEYS + STR_KEYS + NUM_KEYS}
        client = cfg.get("download_client")
        if isinstance(client, dict) and client:
            out["download_client"] = {k: client.get(k, "") for k in ("type", "url", "username", "category", "tags", "save_path", "download_dir")}
            out["download_client"]["password_set"] = bool(client.get("password"))
        else:
            out["download_client"] = None
        out["secrets"] = {k: bool(str(cfg.get(k, "")).strip() not in ("", "#")) for k in SENSITIVE}
        return jsonify(out)

    @app.put("/api/config")
    def config_put():
        body = request.get_json() or {}
        cfg = read_cfg()
        for k in BOOL_KEYS:
            if k in body:
                cfg[k] = bool(body[k])
        for k in STR_KEYS:
            if k in body:
                cfg[k] = str(body[k]).strip()
        limits = {
            "screenshot_count": (1, 200),
            "screenshot_start_seconds": (0, 86400),
            "screenshot_interval_seconds": (1, 86400),
        }
        for k, (minimum, maximum) in limits.items():
            if k in body:
                value = body[k]
                if isinstance(value, bool):
                    abort(400, f"{k} must be an integer")
                try:
                    value = int(value)
                except (TypeError, ValueError, OverflowError):
                    abort(400, f"{k} must be an integer")
                if not minimum <= value <= maximum:
                    abort(400, f"{k} must be between {minimum} and {maximum}")
                cfg[k] = value
        if "download_client" in body:
            incoming = body["download_client"]
            if incoming is None or incoming == {}:
                cfg["download_client"] = None
            elif isinstance(incoming, dict):
                kind = str(incoming.get("type", "")).strip().lower()
                url = str(incoming.get("url", "")).strip()
                if kind not in ("qbittorrent", "transmission") or not url:
                    abort(400, "Download client needs a supported type and URL")
                previous = cfg.get("download_client") if isinstance(cfg.get("download_client"), dict) else {}
                client = {k: str(incoming.get(k, previous.get(k, ""))).strip() for k in ("type", "url", "username", "category", "tags", "save_path", "download_dir")}
                password = incoming.get("password")
                client["password"] = str(password) if isinstance(password, str) and password else str(previous.get("password", ""))
                cfg["download_client"] = client
            else:
                abort(400, "download_client must be an object or null")
        for k in SENSITIVE:  # only overwritten when a new value is sent
            v = body.get("secrets", {}).get(k)
            if isinstance(v, str) and v.strip():
                cfg[k] = v.strip()
        path = ROOT / "config.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
        tmp.replace(path)
        return config_get()

    @app.get("/api/tools")
    def tools():
        return jsonify({t: bool(shutil.which(t)) for t in ("ffmpeg", "ffprobe", "mediainfo")})

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Web UI for the uploader")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--root", default="/", help="Top-level folder the file browser may access")
    ap.add_argument("--jobs", type=int, default=1, help="Concurrent jobs (default 1)")
    a = ap.parse_args()
    token = os.environ.get("AAODF_WEB_TOKEN") or secrets.token_urlsafe(24)
    app = create_app(Path(a.root), token, a.jobs)
    shown = "127.0.0.1" if a.host in ("0.0.0.0", "::") else a.host
    print(f"\n  Open: http://{shown}:{a.port}/?token={token}\n  (root: {Path(a.root).resolve()})\n", flush=True)
    app.run(host=a.host, port=a.port, threaded=True)


if __name__ == "__main__":
    main()
