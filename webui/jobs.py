"""Job queue that drives the existing CLI (`python -m src <target>`) as a subprocess.

The CLI blocks on two stdin prompts (review images, confirm upload). This module
watches its stdout for those prompts and answers them when the user clicks a
button in the UI, so none of the pipeline code needs to change.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from src.core import find_videos, episode_videos, episode_info

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
STATE_DIR = OUTPUT / ".webui"
ACTIVE = {"queued", "running", "awaiting_review", "awaiting_confirm"}
MAX_LOG_LINES = 5000

# Markers printed by the CLI. Kept in one place because the UI depends on them.
M_REVIEW = "Press ENTER to continue"
M_PLAN_START = "Upload details:"
M_PLAN_END = "1) Upload now"
M_CONFIRM = "0) Cancel"

JOB_OPTION_KEYS = ("anonymous_upload", "personal_release", "stream_optimized")


def inspect_target(target: Path) -> dict:
    """Predict what the CLI will do with `target` (same logic as src/cli.py)."""
    if not target.exists():
        return {"ok": False, "error": "Path does not exist"}
    videos = find_videos(target)
    if not videos:
        return {"ok": False, "error": "No .mkv/.mp4 files found"}
    sources = episode_videos(videos)
    first = sources[0]
    multi = len(sources) > 1
    release = target.name if target.is_dir() else first.stem
    name = release if multi else first.stem
    out_dir = f"UPLOAD.{name}"
    return {
        "ok": True,
        "videos": len(videos),
        "sources": len(sources),
        "first": first.name,
        "release": release,
        "name": name,
        "out_dir": out_dir,
        "output_exists": (OUTPUT / out_dir).exists(),
        "tv": any(episode_info(v) for v in videos),
    }


@dataclass
class Job:
    id: str
    target: str
    options: dict
    status: str = "queued"
    stage: str = "queued"
    percent: int = 0
    detail: str = ""
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    name: str | None = None
    out_dir: str | None = None
    link: str | None = None
    uploaded: bool = False
    error: str | None = None
    plan: list = field(default_factory=list)
    frames_total: int = 0
    frames_done: int = 0
    hosted: int = 0
    hosted_total: int = 0
    exit_code: int | None = None
    regenerating: bool = False

    def public(self, log_count: int) -> dict:
        d = asdict(self)
        d["log_count"] = log_count
        return d


class JobManager:
    def __init__(self, max_concurrent: int = 1):
        self.lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.logs: dict[str, list[str]] = {}
        self.procs: dict[str, subprocess.Popen] = {}
        self.cancelled: set[str] = set()
        self.declined: set[str] = set()
        self.max_concurrent = max_concurrent
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "logs").mkdir(exist_ok=True)
        self._load()

    # ---------- persistence ----------
    def _state_file(self) -> Path:
        return STATE_DIR / "state.json"

    def _save(self) -> None:
        with self.lock:
            data = {
                "max_concurrent": self.max_concurrent,
                "jobs": [asdict(self.jobs[i]) for i in self.order if i in self.jobs],
            }
        tmp = self._state_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self._state_file())

    def _load(self) -> None:
        try:
            data = json.loads(self._state_file().read_text(encoding="utf-8"))
        except Exception:
            return
        self.max_concurrent = int(data.get("max_concurrent", self.max_concurrent))
        for d in data.get("jobs", []):
            try:
                j = Job(**d)
            except TypeError:
                continue
            if j.status in ACTIVE:
                j.status, j.stage, j.error = "interrupted", "interrupted", "Server restarted while this job was active"
                j.finished = time.time()
            self.jobs[j.id] = j
            self.order.append(j.id)
            lf = STATE_DIR / "logs" / f"{j.id}.log"
            try:
                self.logs[j.id] = lf.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_LOG_LINES:]
            except OSError:
                self.logs[j.id] = []

    # ---------- public API ----------
    def list(self) -> list[dict]:
        with self.lock:
            return [self.jobs[i].public(len(self.logs.get(i, []))) for i in reversed(self.order)]

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def log_since(self, job_id: str, since: int) -> list[str]:
        with self.lock:
            return self.logs.get(job_id, [])[since:]

    def add(self, paths: list[str], options: dict) -> tuple[list[dict], list[str]]:
        created, skipped = [], []
        with self.lock:
            busy = {j.target for j in self.jobs.values() if j.status in ACTIVE}
            for p in paths:
                if p in busy:
                    skipped.append(p)
                    continue
                job = Job(id=uuid.uuid4().hex[:10], target=p, options=dict(options))
                self.jobs[job.id] = job
                self.order.append(job.id)
                self.logs[job.id] = []
                busy.add(p)
                created.append(job.public(0))
        self._save()
        self._pump()
        return created, skipped

    def set_max_concurrent(self, n: int) -> None:
        self.max_concurrent = max(1, min(8, n))
        self._save()
        self._pump()

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status not in ACTIVE:
                return False
            self.cancelled.add(job_id)
            proc = self.procs.get(job_id)
            if job.status == "queued":
                self._finish(job, "cancelled", "Cancelled while queued")
        if proc:
            self._kill(proc)
        self._save()
        return True

    def retry(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        if not job or job.status in ACTIVE:
            return None
        created, _ = self.add([job.target], job.options)
        return created[0] if created else None

    def remove(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status in ACTIVE:
                return False
            self.jobs.pop(job_id)
            self.order.remove(job_id)
            self.logs.pop(job_id, None)
        (STATE_DIR / "logs" / f"{job_id}.log").unlink(missing_ok=True)
        self._save()
        return True

    def clear_finished(self) -> int:
        with self.lock:
            ids = [i for i in self.order if self.jobs[i].status not in ACTIVE]
        return sum(self.remove(i) for i in ids)

    def proceed(self, job_id: str) -> str | None:
        """Answer the review prompt (ENTER). Returns an error string or None."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status != "awaiting_review":
                return "Job is not waiting for review"
            if job.regenerating:
                return "Frames are still being regenerated"
            if not any((OUTPUT / (job.out_dir or "")).glob("*.png")):
                return "No images left - regenerate frames or cancel the job"
            job.status, job.stage = "running", "hosting"
            self._write(job_id, "\n")
        self._save()
        return None

    def confirm(self, job_id: str, go: bool) -> str | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status != "awaiting_confirm":
                return "Job is not waiting for upload confirmation"
            if go:
                job.status, job.stage, job.error = "running", "uploading", None
            else:
                self.declined.add(job_id)
                job.status, job.stage = "running", "finalize"
            self._write(job_id, "1\n" if go else "0\n")
        self._save()
        return None

    def regenerate(self, job_id: str) -> str | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status != "awaiting_review":
                return "Job is not waiting for review"
            if job.regenerating:
                return "Already regenerating"
            job.regenerating = True
        threading.Thread(target=self._regen_worker, args=(job_id,), daemon=True).start()
        return None

    # ---------- internals ----------
    def _write(self, job_id: str, text: str) -> None:
        proc = self.procs.get(job_id)
        if proc and proc.stdin:
            try:
                proc.stdin.write(text)
                proc.stdin.flush()
            except OSError:
                pass

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def _running_count(self) -> int:
        # Jobs waiting on the user do not hold a slot, so a batch keeps moving.
        return sum(1 for j in self.jobs.values() if j.status == "running")

    def _pump(self) -> None:
        with self.lock:
            for jid in self.order:
                if self._running_count() >= self.max_concurrent:
                    break
                job = self.jobs[jid]
                if job.status == "queued":
                    job.status, job.stage, job.started = "running", "starting", time.time()
                    job.percent = 2
                    threading.Thread(target=self._run, args=(jid,), daemon=True).start()
        self._save()

    def _finish(self, job: Job, status: str, error: str | None = None) -> None:
        job.status, job.finished = status, time.time()
        job.stage = status
        if status in ("finished",) and job.uploaded:
            job.percent = 100
        if error:
            job.error = error

    def _append(self, jid: str, line: str) -> None:
        buf = self.logs.setdefault(jid, [])
        buf.append(line)
        if len(buf) > MAX_LOG_LINES:
            del buf[: len(buf) - MAX_LOG_LINES]
        try:
            with open(STATE_DIR / "logs" / f"{jid}.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _config_for(self, job: Job) -> Path:
        from src.config import DEFAULT_CONFIG, CONFIG_FILE
        cfg = dict(DEFAULT_CONFIG)
        try:
            cfg.update(json.loads(Path(ROOT / "config.json").read_text(encoding="utf-8")))
        except Exception:
            pass
        for k in JOB_OPTION_KEYS:
            if k in job.options:
                cfg[k] = bool(job.options[k])
        # Without auto-upload the CLI shows its confirm prompt, which the UI answers.
        cfg["confirm_before_upload"] = not job.options.get("auto_upload", False)
        path = STATE_DIR / f"cfg-{job.id}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return path

    def _run(self, jid: str) -> None:
        job = self.jobs[jid]
        cfg_path = None
        try:
            info = inspect_target(Path(job.target))
            if not info["ok"]:
                with self.lock:
                    self._append(jid, f"Error: {info['error']}: {job.target}")
                    self._finish(job, "failed", info["error"])
                return
            with self.lock:
                clash = next((j for j in self.jobs.values() if j.id != jid and j.out_dir == info["out_dir"]
                              and j.status in ACTIVE), None)
                if clash:
                    self._append(jid, f"Error: output folder {info['out_dir']} is in use by another active job")
                    self._finish(job, "failed", "Output folder is used by another active job")
                    return
                job.name, job.out_dir = info["name"], info["out_dir"]
                if jid in self.cancelled:
                    self._finish(job, "cancelled", "Cancelled")
                    return
            cfg_path = self._config_for(job)
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONPATH="src", AAODF_CONFIG=str(cfg_path))
            proc = subprocess.Popen(
                [sys.executable, "-m", "src", job.target], cwd=ROOT, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True,
            )
            with self.lock:
                self.procs[jid] = proc
                if jid in self.cancelled:
                    self._kill(proc)
            for raw in proc.stdout:
                with self.lock:
                    self._on_line(job, raw.rstrip("\r\n"))
            rc = proc.wait()
            with self.lock:
                job.exit_code = rc
                if jid in self.cancelled:
                    self._finish(job, "cancelled", "Cancelled")
                elif rc != 0:
                    self._finish(job, "failed", job.error or self._last_line(jid) or f"Exited with code {rc}")
                elif job.uploaded:
                    self._finish(job, "finished")
                else:
                    reason = "Upload declined" if jid in self.declined else (job.error or "Ended without uploading")
                    self._finish(job, "finished", reason)
        except Exception as e:  # noqa: BLE001 - surface any launcher error in the UI
            with self.lock:
                self._append(jid, f"Error: {e}")
                self._finish(job, "failed", str(e))
        finally:
            with self.lock:
                self.procs.pop(jid, None)
            if cfg_path:
                cfg_path.unlink(missing_ok=True)
            self._save()
            self._pump()

    def _last_line(self, jid: str) -> str:
        for line in reversed(self.logs.get(jid, [])):
            if line.strip():
                return line.strip()[:300]
        return ""

    def _on_line(self, job: Job, line: str) -> None:
        jid = job.id
        self._append(jid, line)
        s = line.strip()
        changed = False

        if m := re.match(r"Extracting (\d+) ", s):
            job.stage, job.frames_total, job.frames_done = "frames", int(m.group(1)), 0
            changed = True
        elif re.match(r"Frame\s+\d+/\d+ .*: (done|FAILED)$", s):
            job.frames_done += 1
        elif s.startswith("Extracted ") and " frames to" in s:
            job.detail = s
        elif M_REVIEW in s:
            if job.options.get("review", True):
                job.status, job.stage = "awaiting_review", "review"
                changed = True
            else:
                job.stage = "hosting"
                self._write(job.id, "\n")
        elif m := re.match(r"\[(\d+)/(\d+)\] ", s):
            job.stage, job.hosted, job.hosted_total = "hosting", int(m.group(1)) - 1, int(m.group(2))
            changed = True
        elif m := re.match(r"Uploaded (\d+)/(\d+) images", s):
            job.hosted, job.hosted_total = int(m.group(1)), int(m.group(2))
        elif s.startswith("No PNG files found"):
            job.error = "No images to upload"
        elif s.startswith("TORRENT DETAILS"):
            job.stage = "finalize"
            changed = True
        elif s.startswith(("Searching TMDB", "Matched on TMDB", "No TMDB match")):
            job.stage, job.detail = "finalize", s
            changed = True
        elif s.startswith("Generating nfo"):
            job.stage = "uploading"
            changed = True
        elif s.startswith(M_PLAN_START):
            job.plan = []
            job._collecting = True  # type: ignore[attr-defined]
        elif M_PLAN_END in s:
            job._collecting = False  # type: ignore[attr-defined]
        elif M_CONFIRM in s:
            job.status, job.stage = "awaiting_confirm", "confirm"
            changed = True
        elif getattr(job, "_collecting", False) and s:
            job.plan.append(s)
        elif s.startswith("Upload successful"):
            job.uploaded = True
            job.error = None
        elif s.startswith("Link:"):
            job.link = s.split(":", 1)[1].strip()
        elif s.startswith(("Upload failed", "Response:")):
            job.error = s[:300]
        elif s.endswith("skipping tracker upload.") or s.startswith("Error:"):
            job.error = s[:300]

        job.percent = self._percent(job)
        if changed:
            self._save()

    @staticmethod
    def _percent(j: Job) -> int:
        frac = lambda a, b: (min(a, b) / b) if b else 0
        return {
            "queued": 0, "starting": 2,
            "frames": 5 + int(35 * frac(j.frames_done, j.frames_total)),
            "review": 40,
            "hosting": 40 + int(40 * frac(j.hosted, j.hosted_total)),
            "finalize": 85, "confirm": 92, "uploading": 96,
        }.get(j.stage, j.percent)

    def _regen_worker(self, jid: str) -> None:
        job = self.jobs[jid]
        try:
            cmd = [sys.executable, "-m", "webui.regen", job.target, "--name", job.name or "", "--out", str(OUTPUT / (job.out_dir or ""))]
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                                    env=dict(os.environ, PYTHONUNBUFFERED="1"))
            for line in proc.stdout:
                with self.lock:
                    self._append(jid, "[regen] " + line.rstrip())
            proc.wait()
        finally:
            with self.lock:
                job.regenerating = False
