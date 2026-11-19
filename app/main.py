"""Web server: accepts a YouTube link, runs the clipping pipeline in the
background, and serves progress and finished clips to the page."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.background import BackgroundTask

from . import config, render, selector, source, trim, youtube

app = FastAPI(title="Clipsmith")


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"} | {
    h.strip().lower() for h in os.environ.get("CLIPSMITH_ALLOWED_HOSTS", "").split(",") if h.strip()
}


def _hostname(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8765
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """Only this computer's own web pages may use the app.

    - Host check (DNS rebinding): a site can point its own name at 127.0.0.1,
      making its requests look same-origin. Requests not addressed to
      127.0.0.1 / localhost are refused, reads included.
    - Origin check (CSRF): browsers send Origin on cross-site POST/DELETE; a
      page on another site must not delete videos, post clips or disconnect
      YouTube. Extra hostnames can be allowed with CLIPSMITH_ALLOWED_HOSTS."""
    if _hostname(request.headers.get("host", "")) not in _LOCAL_HOSTS:
        return JSONResponse({"detail": "Open Clipsmith at http://127.0.0.1:8765."}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Request from another website refused."}, status_code=403)
    return await call_next(request)

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=1)  # one video at a time; encoding uses every core
_upload_pool = ThreadPoolExecutor(max_workers=1)  # YouTube uploads don't wait behind rendering

_YT = re.compile(r"^https?://([\w-]+\.)?(youtube\.com|youtu\.be)/", re.I)


class JobRequest(BaseModel):
    url: str
    clip_count: int = Field(default=5, ge=1, le=15)
    min_len: int = Field(default=30, ge=10, le=170)
    max_len: int = Field(default=60, ge=15, le=180)
    style: str = "yellow"
    layout: str = "auto"
    hook: bool = True
    prompt: str = Field(default="", max_length=300)
    start_time: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end_time: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    aspect: str = "9:16"
    trim_pauses: bool = True


_JOB_ID = re.compile(r"[0-9a-f]{10}")  # uuid4().hex[:10], see _new_job


def _job_dir(job_id: str) -> Path:
    """A job's folder. Job IDs come from URLs, so anything that isn't exactly a
    generated ID is refused before it can become part of a file path."""
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValueError("Not a valid job ID.")
    # Belt and braces: resolve the path and confirm it is still inside the jobs folder.
    root = os.path.realpath(config.JOBS_DIR)
    path = os.path.realpath(os.path.join(root, job_id))
    if not path.startswith(root + os.sep):
        raise ValueError("Not a valid job ID.")
    return Path(path)


def _save(job: dict) -> None:
    d = _job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / "job.json")


def _update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs[job_id]
        job.update(fields)
        _save(job)


def _load_existing() -> None:
    for f in config.JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not _JOB_ID.fullmatch(str(job.get("id", ""))):
            continue  # not a job this app created
        if job.get("status") in ("queued", "running"):
            job["status"] = "error"
            job["error"] = "The app was closed while this was processing."
        for clip in job.get("clips") or []:
            yt = clip.get("youtube")
            if yt and yt.get("status") in ("queued", "uploading"):
                clip["youtube"] = {"status": "error", "error": "The app was closed during the upload. Post it again."}
        _jobs[job["id"]] = job


_load_existing()


def _clock(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def friendly_error(exc: BaseException) -> str:
    """Turn a pipeline exception (often raw yt-dlp output) into a message for the page."""
    text = _ANSI.sub("", str(exc)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    low = text.lower()
    if "getaddrinfo" in low or "failed to resolve" in low or "connection refused" in low or "timed out" in low:
        return "Couldn't reach YouTube. Check your internet connection and try again."
    if "429" in low or "too many requests" in low:
        return "YouTube is limiting requests from your connection right now. Wait a few minutes and try again."
    text = re.sub(r"^\[\w+\]\s*[\w-]+:\s*", "", text)  # drop yt-dlp's "[youtube] <id>: " prefix
    return text or type(exc).__name__


def _render_bounds(
    clip_start: float,
    clip_end: float,
    words: list,
    duration: float | None,
    range_start: float | None = None,
    range_end: float | None = None,
) -> tuple[float, float]:
    """Pad a clip slightly so its first word isn't cut and the moment can land:
    run on into the pause after the last word (a laugh, a reaction, engine
    noise), never past the video or outside a requested From/To range."""
    start = max(0.0, clip_start - 0.15)
    next_word = next((w.start for w in words if w.start >= clip_end - 0.05), None)
    tail = 1.5 if next_word is None else max(0.3, min(1.5, next_word - clip_end - 0.1))
    end = clip_end + tail
    if duration:
        end = min(end, duration)
    if range_start is not None:
        start = max(start, range_start)
    if range_end is not None:
        end = min(end, range_end)
    return round(start, 3), round(end, 3)


def _run(job_id: str, req: JobRequest) -> None:
    workdir = _job_dir(job_id)

    last = {"pct": -1.0, "msg": ""}

    def progress(pct: float, message: str) -> None:
        pct = round(min(pct, 99), 1)
        # the download hook fires many times a second; don't rewrite job.json each time
        if message == last["msg"] and abs(pct - last["pct"]) < 1:
            return
        last.update(pct=pct, msg=message)
        _update(job_id, progress=pct, message=message)

    try:
        _update(job_id, status="running")
        upload = _jobs[job_id].get("upload")
        if upload:
            path = next(workdir.glob("source.*"))
            src = source.from_local_file(path, workdir, progress, title=Path(upload).stem)
        else:
            src = source.download(req.url, workdir, progress)
        _update(job_id, title=src.title, duration=src.duration, transcript_source=src.transcript_source)

        progress(42, "Finding the best moments")
        sentences = selector.within(selector.to_sentences(src.words), req.start_time, req.end_time)
        clips, method, warning = selector.select(
            sentences, src.title, req.clip_count, req.min_len, req.max_len, progress, prompt=req.prompt
        ) if sentences else ([], "basic", None)
        if not clips:
            ranged = req.start_time is not None or req.end_time is not None
            where = (
                f"between {_clock(req.start_time or 0)} and "
                f"{_clock(req.end_time) if req.end_time is not None else 'the end'} of this video"
                if ranged else "in this video"
            )
            raise RuntimeError(
                f"No stretch of speech between {req.min_len} and {req.max_len} seconds was found {where}."
            )
        _update(job_id, selection=method, warning=warning)

        done: list[dict] = []
        for i, clip in enumerate(clips):
            progress(45 + 54 * i / len(clips), f"Rendering clip {i + 1} of {len(clips)}")
            start, end = _render_bounds(
                clip.start, clip.end, src.words, src.duration, req.start_time, req.end_time
            )
            name = f"clip_{i + 1:02d}"
            keep = trim.plan(src.video_path, src.words, start, end) if req.trim_pauses else None
            info = render.render_clip(
                src.video_path, src.words, start, end, workdir, name,
                style=req.style, layout=req.layout, hook=clip.hook if req.hook else None, aspect=req.aspect,
                keep=keep,
            )
            done.append(
                asdict(clip)
                | {
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "length": round(info.get("length", end - start), 1),
                    "video": f"/media/{job_id}/{name}.mp4",
                    "thumb": f"/media/{job_id}/{name}.jpg",
                    "layout": info["layout"],
                    "aspect": req.aspect,
                }
            )
            _update(job_id, clips=list(done))

        src.video_path.unlink(missing_ok=True)  # the source is large; clips are what matter
        for leftover in workdir.glob("source.*"):
            leftover.unlink(missing_ok=True)
        _update(job_id, status="done", progress=100, message="Done")
    except Exception as exc:
        traceback.print_exc()
        _update(job_id, status="error", error=friendly_error(exc)[:500])


@app.get("/api/status")
def status() -> dict:
    return {
        "ai": config.ai_provider(),
        "missing": config.missing_tools(),
        "model": {
            "gemini": config.GEMINI_MODEL,
            "claude": config.CLAUDE_MODEL,
            "ollama": f"{config.OLLAMA_MODEL} (local)",
        }.get(config.ai_provider()),
        "styles": {k: v["label"] for k, v in render.STYLES.items()},
        "style_looks": {
            k: {"font": v["font"], "active": v["css"], "upper": v["upper"], "width": v["cw"],
                "reveal": bool(v.get("reveal"))} for k, v in render.STYLES.items()
        },
    }


def _check_options(req: JobRequest) -> None:
    if req.min_len >= req.max_len:
        raise HTTPException(400, "Minimum length must be shorter than maximum length.")
    if req.style not in render.STYLES:
        raise HTTPException(400, "Unknown caption style.")
    if req.layout not in ("auto", "fill", "fit", "split"):
        raise HTTPException(400, "Unknown layout.")
    if req.start_time is not None and req.end_time is not None and req.end_time <= req.start_time:
        raise HTTPException(400, "The 'To' time must be after the 'From' time.")
    if req.aspect not in render.ASPECTS:
        raise HTTPException(400, "Unknown aspect ratio.")


def _new_job(req: JobRequest, job_id: str | None = None, **extra) -> dict:
    job_id = job_id or uuid.uuid4().hex[:10]
    job = {
        "id": job_id, "url": req.url.strip(), "status": "queued", "progress": 0, "message": "Waiting to start",
        "title": None, "clips": [], "error": None, "warning": None, "selection": None,
        "created": time.time(), "options": req.model_dump(exclude={"url"}),
    } | extra
    with _lock:
        _jobs[job_id] = job
        _save(job)
    _pool.submit(_run, job_id, req)
    return job


@app.post("/api/jobs")
def create_job(req: JobRequest) -> dict:
    if not _YT.match(req.url.strip()):
        raise HTTPException(400, "That doesn't look like a YouTube link.")
    _check_options(req)
    return _new_job(req)


_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
_MAX_UPLOAD_BYTES = 8 * 1024**3


@app.post("/api/jobs/upload")
async def upload_job(
    file: UploadFile = File(...),
    clip_count: int = Form(5),
    min_len: int = Form(30),
    max_len: int = Form(60),
    style: str = Form("yellow"),
    layout: str = Form("auto"),
    hook: bool = Form(True),
    prompt: str = Form(""),
    start_time: float | None = Form(None),
    end_time: float | None = Form(None),
    aspect: str = Form("9:16"),
    trim_pauses: bool = Form(True),
) -> dict:
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in _VIDEO_EXTS:
        raise HTTPException(400, "Please upload a video file: mp4, mov, mkv or webm.")
    try:
        req = JobRequest(url="", clip_count=clip_count, min_len=min_len, max_len=max_len,
                         style=style, layout=layout, hook=hook,
                         prompt=prompt, start_time=start_time, end_time=end_time, aspect=aspect,
                         trim_pauses=trim_pauses)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from None
    _check_options(req)
    job_id = uuid.uuid4().hex[:10]
    d = _job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with open(d / f"source{ext}", "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"File is too large (limit {_MAX_UPLOAD_BYTES // 1024**3} GB).")
                out.write(chunk)
    except BaseException:  # includes a cancelled/interrupted upload
        shutil.rmtree(d, ignore_errors=True)
        raise
    return _new_job(req, job_id, upload=name)


@app.get("/api/jobs/{job_id}/zip")
def download_zip(job_id: str) -> FileResponse:
    with _lock:
        job = _jobs.get(job_id)
        # Copy the list: a clip can be deleted while the zip is being built.
        clips = list(job.get("clips") or []) if job else []
    if not job:
        raise HTTPException(404, "Job not found.")
    d = _job_dir(job_id)
    files = [d / Path(c["video"]).name for c in clips]
    files = [f for f in files if f.is_file()]
    if not files:
        raise HTTPException(409, "This video has no finished clips yet.")
    # Build in a throwaway file (outside the served /media tree) and delete it once sent,
    # so a download doesn't leave a second copy of every clip on disk.
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=config.DATA_DIR)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for f in files:
                try:
                    z.write(f, f.name)
                except FileNotFoundError:
                    continue  # deleted from another tab while zipping
    except BaseException:
        os.unlink(tmp)
        raise
    title = re.sub(r'[\\/:*?"<>|]+', "", job.get("title") or "clips").strip()[:80] or "clips"
    return FileResponse(tmp, media_type="application/zip", filename=f"{title}.zip",
                        background=BackgroundTask(os.unlink, tmp))


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    with _lock:
        return sorted(_jobs.values(), key=lambda j: -j["created"])


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        if job["status"] in ("queued", "running"):
            raise HTTPException(409, "This video is still processing.")
        if any((c.get("youtube") or {}).get("status") in ("queued", "uploading") for c in job.get("clips") or []):
            raise HTTPException(409, "A clip from this video is being posted to YouTube. Wait for it to finish.")
        del _jobs[job_id]
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}/clips/{index}")
def delete_clip(job_id: str, index: int) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        clips = job.get("clips") or []
        if not 0 <= index < len(clips):
            raise HTTPException(404, "Clip not found.")
        if job["status"] in ("queued", "running"):
            raise HTTPException(409, "This video is still processing.")
        # Uploads find their clip by position, so removing one would point them at the wrong clip.
        if any((c.get("youtube") or {}).get("status") in ("queued", "uploading") for c in clips):
            raise HTTPException(409, "A clip from this video is being posted to YouTube. Wait for it to finish.")
        clip = clips.pop(index)
        _save(job)
    d = _job_dir(job_id)
    for url in (clip.get("video"), clip.get("thumb")):
        if url:
            (d / Path(url).name).unlink(missing_ok=True)
    return {"ok": True}


# ---------- YouTube ----------

class PublishRequest(BaseModel):
    title: str = Field(max_length=500)
    description: str = Field(default="", max_length=10000)
    hashtags: list[str] = Field(default_factory=list, max_length=100)
    privacy: str = "public"
    publish_at: datetime | None = None
    made_for_kids: bool = False
    notify_subscribers: bool = True
    add_source_link: bool = False
    # The clip's video path when the dialog opened. Clips are addressed by position, so if one
    # was deleted meanwhile this catches a post landing on a different clip.
    clip_video: str | None = None


@app.get("/api/youtube")
def youtube_status() -> dict:
    return youtube.status()


@app.get("/api/youtube/connect")
def youtube_connect(request: Request) -> RedirectResponse:
    # Google documents the desktop loopback redirect as http://127.0.0.1:port with
    # no path, so Google sends the user back to this app's root URL (see index()).
    redirect = str(request.base_url)
    try:
        return RedirectResponse(youtube.start_auth(redirect), status_code=307)
    except youtube.NotConfigured as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/youtube/disconnect")
def youtube_disconnect() -> dict:
    youtube.disconnect()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/clips/{index}/youtube")
def publish_clip(job_id: str, index: int, req: PublishRequest) -> dict:
    if not youtube.status()["connected"]:
        raise HTTPException(409, "Connect YouTube first.")
    with _lock:
        job = _jobs.get(job_id)
        clips = (job or {}).get("clips") or []
        if not job or not 0 <= index < len(clips):
            raise HTTPException(404, "Clip not found.")
        clip = clips[index]
        if req.clip_video is not None and req.clip_video != clip.get("video"):
            raise HTTPException(409, "This clip changed since you opened it (another clip was deleted). Reload the page and try again.")
        if (clip.get("youtube") or {}).get("status") in ("queued", "uploading"):
            raise HTTPException(409, "This clip is already being posted.")
        source_url = job.get("url") if req.add_source_link and _YT.match(job.get("url") or "") else None
        try:
            meta = youtube.build_metadata(
                title=req.title, description=req.description, hashtags=req.hashtags, privacy=req.privacy,
                publish_at=req.publish_at, made_for_kids=req.made_for_kids, source_url=source_url,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        clip["youtube"] = {"status": "queued", "progress": 0}
        _save(job)
    _upload_pool.submit(_publish, job_id, index, meta, req.notify_subscribers)
    return clip["youtube"]


def _set_youtube(job_id: str, index: int, value: dict) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job and index < len(job.get("clips") or []):
            job["clips"][index]["youtube"] = value
            _save(job)


def _publish(job_id: str, index: int, meta: dict, notify: bool) -> None:
    with _lock:
        job = _jobs.get(job_id)
        clip = job["clips"][index] if job else None
    if not clip:
        return
    path = _job_dir(job_id) / Path(clip["video"]).name
    last = {"pct": -10}

    def progress(pct: int) -> None:
        if pct - last["pct"] >= 5 or pct == 100:  # don't rewrite job.json for every chunk
            last["pct"] = pct
            _set_youtube(job_id, index, {"status": "uploading", "progress": pct})

    try:
        _set_youtube(job_id, index, {"status": "uploading", "progress": 0})
        video_id = youtube.upload(path, meta, notify_subscribers=notify, progress=progress)
        result = {"status": "done", "video_id": video_id, "url": f"https://youtu.be/{video_id}"}
        if meta.get("status", {}).get("publishAt"):
            result["scheduled_for"] = meta["status"]["publishAt"]
        else:
            result["privacy"] = meta.get("status", {}).get("privacyStatus")
        _set_youtube(job_id, index, result)
    except youtube.UploadError as exc:
        _set_youtube(job_id, index, {"status": "error", "error": str(exc)})
    except Exception as exc:
        traceback.print_exc()
        _set_youtube(job_id, index, {"status": "error", "error": f"Upload failed: {exc}"[:300]})


app.mount("/fonts", StaticFiles(directory=render.FONTS_DIR), name="fonts")
app.mount("/media", StaticFiles(directory=config.JOBS_DIR), name="media")


@app.get("/")
def index(state: str | None = None, code: str | None = None, error: str | None = None):
    # Google sends the browser back here after the YouTube sign-in.
    if error and state:
        reason = "access_denied" if error == "access_denied" else "failed"  # never echo the raw value
        return RedirectResponse(f"/?youtube=error&reason={reason}", status_code=303)
    if state and code:
        try:
            youtube.finish_auth(state, code)
        except youtube.AuthError:
            return RedirectResponse("/?youtube=error&reason=expired", status_code=303)
        return RedirectResponse("/?youtube=connected", status_code=303)
    return FileResponse(config.STATIC_DIR / "index.html")
