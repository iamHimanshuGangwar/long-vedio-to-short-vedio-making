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
import hmac
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.background import BackgroundTask

from . import accounts, config, render, selector, source, trim

app = FastAPI(title="ReelCraft AI")


_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"} | {
    h.strip().lower() for h in os.environ.get("CLIPSMITH_ALLOWED_HOSTS", "").split(",") if h.strip()
}
_RENDER_HOST = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip().lower()
if _RENDER_HOST:
    _ALLOWED_HOSTS.add(_RENDER_HOST)
_ADMIN_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_ADMIN_LOGIN_LOCK = threading.Lock()


def _hostname(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8765
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """Allow only configured hosts and same-origin state-changing browser requests."""
    host = _hostname(request.headers.get("host", ""))
    if host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "This host is not configured for ReelCraft AI."}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin:
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
            expected_origin = f"{scheme}://{request.headers.get('host', '')}".rstrip("/")
            if origin.rstrip("/") != expected_origin:
                return JSONResponse({"detail": "Request from another website refused."}, status_code=403)
    return await call_next(request)

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=1)  # one video at a time; encoding uses every core

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


class GoogleCredential(BaseModel):
    credential: str = Field(max_length=8192)


class AdminLogin(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)


class PaymentOrder(BaseModel):
    plan: str
    billing: str


class PaymentVerification(BaseModel):
    razorpay_order_id: str = Field(pattern=r"^order_[A-Za-z0-9]+$")
    razorpay_payment_id: str = Field(pattern=r"^pay_[A-Za-z0-9]+$")
    razorpay_signature: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class AdminPlanUpdate(BaseModel):
    plan: str
    billing: str = "monthly"


class AdminBanUpdate(BaseModel):
    banned: bool


class AdminLimitUpdate(BaseModel):
    max_source_minutes: int = Field(ge=1, le=10)


def _session_cookie(response: Response, user_id: str, request: Request) -> None:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip().lower()
    response.set_cookie(
        accounts.SESSION_COOKIE, accounts.make_session(user_id), httponly=True,
        secure=scheme == "https",
        samesite="lax", max_age=60 * 60 * 24 * 14, path="/",
    )


@app.post("/api/auth/google")
def google_auth(body: GoogleCredential, request: Request, response: Response) -> dict:
    user = accounts.google_login(body.credential)
    _session_cookie(response, str(user["_id"]), request)
    return accounts._public_user(user)


@app.post("/api/admin/login")
def admin_login(body: AdminLogin, request: Request, response: Response) -> dict:
    if not config.ADMIN_EMAIL or not config.ADMIN_PASSWORD:
        raise HTTPException(503, "Administrator login is not configured.")
    now = time.time()
    address = request.client.host if request.client else "unknown"
    with _ADMIN_LOGIN_LOCK:
        recent = [t for t in _ADMIN_LOGIN_ATTEMPTS.get(address, []) if now - t < 900]
        _ADMIN_LOGIN_ATTEMPTS[address] = recent
        if len(recent) >= 5:
            raise HTTPException(429, "Too many administrator sign-in attempts. Try again in 15 minutes.")
    if len(config.ADMIN_PASSWORD) < 16:
        raise HTTPException(503, "ADMIN_PASSWORD must be at least 16 characters. Rotate and configure a strong secret.")
    if not hmac.compare_digest(body.email.strip().lower(), config.ADMIN_EMAIL) or not hmac.compare_digest(
        body.password, config.ADMIN_PASSWORD
    ):
        with _ADMIN_LOGIN_LOCK:
            _ADMIN_LOGIN_ATTEMPTS.setdefault(address, []).append(now)
        raise HTTPException(401, "Invalid administrator credentials.")
    with _ADMIN_LOGIN_LOCK:
        _ADMIN_LOGIN_ATTEMPTS.pop(address, None)
    db = accounts.mongo_db()
    user = db.users.find_one({"email": config.ADMIN_EMAIL})
    if not user:
        user = {
            "email": config.ADMIN_EMAIL, "name": "Administrator", "role": "admin", "plan": "gold",
            "used": 0, "cycle": "", "banned": False,
            "created_at": datetime.now().astimezone(),
        }
        db.users.insert_one(user)
    else:
        db.users.update_one({"_id": user["_id"]}, {"$set": {"role": "admin"}})
        user["role"] = "admin"
    _session_cookie(response, str(user["_id"]), request)
    return accounts._public_user(user)


@app.post("/api/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(accounts.SESSION_COOKIE, path="/", httponly=True, samesite="lax")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict | None:
    try:
        user = accounts.current_user(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise
    plan = accounts.active_plan(user)
    return accounts._public_user(user) | {
        "plan": plan, "max_minutes": min(accounts.server_max_minutes(), accounts.PLANS[plan]["minutes"]),
    }


@app.get("/api/plans")
def plans() -> dict:
    return {"plans": accounts.PLANS}


@app.post("/api/payments/order")
def create_payment_order(body: PaymentOrder, user: dict = Depends(accounts.current_user)) -> dict:
    if body.plan not in ("normal", "premium", "gold") or body.billing not in ("monthly", "yearly"):
        raise HTTPException(400, "Choose a valid paid plan and billing period.")
    if not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
        raise HTTPException(503, "Payments are not configured. Add Razorpay credentials in the deployment environment.")
    import requests

    amount_rupees = accounts.PLANS[body.plan][body.billing]
    result = requests.post(
        "https://api.razorpay.com/v1/orders",
        auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET),
        json={"amount": amount_rupees * 100, "currency": "INR", "receipt": uuid.uuid4().hex[:40],
              "notes": {"user_id": str(user["_id"]), "plan": body.plan, "billing": body.billing}},
        timeout=15,
    )
    if not result.ok:
        raise HTTPException(502, "Could not create a Razorpay order.")
    order = result.json()
    accounts.mongo_db().orders.insert_one({
        "_id": order["id"], "user_id": user["_id"], "plan": body.plan, "billing": body.billing,
        "amount": amount_rupees * 100, "created_at": datetime.now().astimezone(),
    })
    return {"order_id": order["id"], "amount": order["amount"], "currency": order["currency"],
            "key_id": config.RAZORPAY_KEY_ID}


@app.post("/api/payments/verify")
def verify_payment(body: PaymentVerification, user: dict = Depends(accounts.current_user)) -> dict:
    if not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
        raise HTTPException(503, "Payments are not configured.")
    expected = hmac.new(
        config.RAZORPAY_KEY_SECRET.encode(),
        f"{body.razorpay_order_id}|{body.razorpay_payment_id}".encode(), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, body.razorpay_signature):
        raise HTTPException(400, "Payment signature verification failed.")
    db = accounts.mongo_db()
    order = db.orders.find_one({"_id": body.razorpay_order_id, "user_id": user["_id"]})
    if not order:
        raise HTTPException(404, "Payment order not found.")
    import requests

    response = requests.get(
        f"https://api.razorpay.com/v1/payments/{body.razorpay_payment_id}",
        auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET), timeout=15,
    )
    if not response.ok:
        raise HTTPException(502, "Could not confirm payment status with Razorpay.")
    payment = response.json()
    if (
        payment.get("order_id") != order["_id"]
        or int(payment.get("amount", -1)) != order["amount"]
        or payment.get("currency") != "INR"
    ):
        raise HTTPException(400, "The payment does not match the order.")
    if payment.get("status") == "authorized":
        captured = requests.post(
            f"https://api.razorpay.com/v1/payments/{body.razorpay_payment_id}/capture",
            auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET),
            json={"amount": order["amount"], "currency": "INR"}, timeout=15,
        )
        if not captured.ok:
            raise HTTPException(502, "Razorpay authorized the payment but capture failed. Contact support before retrying.")
        payment = captured.json()
    if payment.get("status") != "captured":
        raise HTTPException(409, "Payment is not captured yet.")
    claim = db.orders.update_one(
        {"_id": order["_id"], "user_id": user["_id"], "paid": {"$ne": True}},
        {"$set": {"paid": True, "payment_id": body.razorpay_payment_id}},
    )
    if claim.modified_count:
        now = datetime.now(timezone.utc)
        days = 365 if order["billing"] == "yearly" else 30
        activated = db.users.update_one({"_id": user["_id"], "banned": {"$ne": True}}, {"$set": {
            "plan": order["plan"], "billing": order["billing"], "plan_expires": now + timedelta(days=days),
            "used": 0, "cycle": "",
        }})
        if not activated.matched_count:
            db.orders.update_one(
                {"_id": order["_id"], "payment_id": body.razorpay_payment_id},
                {"$set": {"paid": False}, "$unset": {"payment_id": ""}},
            )
            raise HTTPException(404, "Account not found while activating the plan.")
    else:
        paid_order = db.orders.find_one({"_id": order["_id"], "user_id": user["_id"], "paid": True})
        if not paid_order or paid_order.get("payment_id") != body.razorpay_payment_id:
            raise HTTPException(409, "This order has already been processed.")
    return {"ok": True}


@app.get("/api/admin/users")
def admin_users(user: dict = Depends(accounts.admin_user)) -> list[dict]:
    users = accounts.mongo_db().users.find({}, {"email": 1, "name": 1, "role": 1, "plan": 1, "billing": 1,
                                                  "used": 1, "cycle": 1, "banned": 1})
    return [accounts._public_user(item) for item in users]


@app.patch("/api/admin/users/{email}/plan")
def admin_update_plan(email: str, body: AdminPlanUpdate, user: dict = Depends(accounts.admin_user)) -> dict:
    if body.plan not in accounts.PLANS or body.billing not in ("monthly", "yearly"):
        raise HTTPException(400, "Invalid plan or billing period.")
    result = accounts.mongo_db().users.update_one({"email": email.lower()}, {"$set": {
        "plan": body.plan, "billing": body.billing, "used": 0, "cycle": "",
        "plan_expires": datetime.now().astimezone() + timedelta(days=365 if body.billing == "yearly" else 30),
    }})
    if not result.matched_count:
        raise HTTPException(404, "User not found.")
    return {"ok": True}


@app.patch("/api/admin/users/{email}/ban")
def admin_ban_user(email: str, body: AdminBanUpdate, user: dict = Depends(accounts.admin_user)) -> dict:
    if email.lower() == config.ADMIN_EMAIL:
        raise HTTPException(400, "The administrator account cannot be banned.")
    result = accounts.mongo_db().users.update_one({"email": email.lower()}, {"$set": {"banned": body.banned}})
    if not result.matched_count:
        raise HTTPException(404, "User not found.")
    return {"ok": True}


@app.post("/api/admin/users/{email}/reset")
def admin_reset_user(email: str, user: dict = Depends(accounts.admin_user)) -> dict:
    result = accounts.mongo_db().users.update_one(
        {"email": email.lower()}, {"$set": {"used": 0, "cycle": ""}}
    )
    if not result.matched_count:
        raise HTTPException(404, "User not found.")
    return {"ok": True}


@app.put("/api/admin/settings/max-source-minutes")
def admin_update_limit(body: AdminLimitUpdate, user: dict = Depends(accounts.admin_user)) -> dict:
    accounts.mongo_db().settings.update_one(
        {"_id": "max_source_minutes"}, {"$set": {"value": body.max_source_minutes}}, upsert=True
    )
    return {"max_source_minutes": body.max_source_minutes}


def _delete_cloudinary_assets(clips: list[dict]) -> None:
    if os.environ.get("APP_ENV") != "production":
        return
    assets_to_delete = [
        (public_id, resource_type)
        for clip in clips
        for key, resource_type in (("video", "video"), ("thumb", "image"))
        if (public_id := (clip.get("cloudinary") or {}).get(key))
    ]
    if not assets_to_delete:
        return
    if not config.CLOUDINARY_URL:
        raise HTTPException(503, "Cloudinary storage is not configured.")
    import cloudinary
    import cloudinary.uploader

    cloudinary.config(url=config.CLOUDINARY_URL, secure=True)
    for public_id, resource_type in assets_to_delete:
        try:
            result = cloudinary.uploader.destroy(
                public_id, resource_type=resource_type, type="authenticated", invalidate=True,
            )
        except Exception as exc:
            raise HTTPException(502, "Could not delete the clip from Cloudinary. Please retry.") from exc
        if result.get("result") not in ("ok", "not found"):
            raise HTTPException(502, "Cloudinary did not confirm clip deletion. Please retry.")


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
    accounts.persist_job(job)


def _update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs[job_id]
        job.update(fields)
        _save(job)


def _recover_interrupted_job(job: dict) -> None:
    if job.get("status") not in ("queued", "running"):
        return
    job["status"] = "error"
    job["error"] = "Processing was interrupted when the server restarted. Start a new job to try again."
    reserved = int(job.get("quota_reserved", 0))
    remaining = reserved - len(job.get("clips") or [])
    if not job.get("quota_refunded") and remaining > 0:
        accounts.refund_quota(job, remaining)
        job["quota_refunded"] = True
    _save(job)


def _load_existing() -> None:
    for f in config.JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not _JOB_ID.fullmatch(str(job.get("id", ""))):
            continue  # not a job this app created
        _recover_interrupted_job(job)
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
        job_meta = _jobs[job_id]
        upload = job_meta.get("upload")
        plan = accounts.PLANS.get(job_meta.get("plan", "free"), accounts.PLANS["free"])
        max_seconds = min(accounts.server_max_minutes(), plan["minutes"]) * 60
        if upload:
            path = next(workdir.glob("source.*"))
            src = source.from_local_file(path, workdir, progress, title=Path(upload).stem,
                                         max_duration_seconds=max_seconds)
        else:
            src = source.download(req.url, workdir, progress, max_duration_seconds=max_seconds)
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
            video_path = workdir / f"{name}.mp4"
            thumb_path = workdir / f"{name}.jpg"
            if os.environ.get("APP_ENV") == "production":
                if not config.CLOUDINARY_URL:
                    raise RuntimeError("Clip storage is not configured. Set CLOUDINARY_URL.")
                import cloudinary
                import cloudinary.utils
                import cloudinary.uploader

                cloudinary.config(url=config.CLOUDINARY_URL, secure=True)
                remote = None
                thumb = None
                try:
                    remote = cloudinary.uploader.upload(
                        str(video_path), resource_type="video", type="authenticated",
                        folder=f"reelcraft/{job_id}", use_filename=True,
                    )
                    thumb = cloudinary.uploader.upload(
                        str(thumb_path), resource_type="image", type="authenticated",
                        folder=f"reelcraft/{job_id}", use_filename=True,
                    )
                    video_url = cloudinary.utils.cloudinary_url(
                        remote["public_id"], resource_type="video", type="authenticated",
                        sign_url=True, secure=True, format="mp4",
                    )[0]
                    thumb_url = cloudinary.utils.cloudinary_url(
                        thumb["public_id"], resource_type="image", type="authenticated",
                        sign_url=True, secure=True, format="jpg",
                    )[0]
                except Exception:
                    for asset, resource_type in ((remote, "video"), (thumb, "image")):
                        if asset and asset.get("public_id"):
                            cloudinary.uploader.destroy(
                                asset["public_id"], resource_type=resource_type, type="authenticated",
                                invalidate=True,
                            )
                    raise
                video_path.unlink(missing_ok=True)
                thumb_path.unlink(missing_ok=True)
            else:
                video_url, thumb_url = f"/media/{job_id}/{name}.mp4", f"/media/{job_id}/{name}.jpg"
            done.append(
                asdict(clip)
                | {
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "length": round(info.get("length", end - start), 1),
                    "video": video_url,
                    "thumb": thumb_url,
                    "cloudinary": (
                        {"video": remote["public_id"], "thumb": thumb["public_id"]}
                        if os.environ.get("APP_ENV") == "production" else None
                    ),
                    "layout": info["layout"],
                    "aspect": req.aspect,
                }
            )
            _update(job_id, clips=list(done))

        src.video_path.unlink(missing_ok=True)  # the source is large; clips are what matter
        for leftover in workdir.glob("source.*"):
            leftover.unlink(missing_ok=True)
        reserved = int(job_meta.get("quota_reserved", 0))
        if reserved > len(done):
            accounts.refund_quota(job_meta, reserved - len(done))
            _update(job_id, quota_refunded=True)
        _update(job_id, status="done", progress=100, message="Done")
    except Exception as exc:
        traceback.print_exc()
        job_meta = _jobs.get(job_id, {})
        if not job_meta.get("quota_refunded") and job_meta.get("quota_reserved"):
            refund = int(job_meta["quota_reserved"]) - len(job_meta.get("clips") or [])
            if refund > 0:
                try:
                    accounts.refund_quota(job_meta, refund)
                    _update(job_id, quota_refunded=True)
                except Exception:
                    traceback.print_exc()
        _update(job_id, status="error", error=friendly_error(exc)[:500])
    finally:
        if os.environ.get("APP_ENV") == "production":
            shutil.rmtree(workdir, ignore_errors=True)


@app.get("/api/status")
def status() -> dict:
    return {
        "ai": config.ai_provider(),
        "auth_enabled": os.environ.get("REELCRAFT_TEST_MODE") != "1" or os.environ.get("APP_ENV") == "production",
        "google_client_id": config.GOOGLE_CLIENT_ID,
        "max_source_minutes": accounts.server_max_minutes(),
        "plans": accounts.PLANS,
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


@app.get("/health")
def health() -> dict:
    if os.environ.get("APP_ENV") == "production":
        accounts.mongo_db().command("ping")
    return {"ok": True}


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


def _plan_request(req: JobRequest, user: dict) -> tuple[JobRequest, str, int]:
    plan_key = accounts.active_plan(user)
    plan = accounts.PLANS[plan_key]
    max_minutes = min(accounts.server_max_minutes(), plan["minutes"])
    if plan_key == "free":
        free = accounts.FREE_SETTINGS
        if not (free["min_len"] <= req.min_len < req.max_len <= free["max_len"]):
            raise HTTPException(403, "The Free plan supports clip lengths from 15 to 30 seconds.")
        req = req.model_copy(update={
            "clip_count": free["clip_count"], "style": "yellow", "layout": free["layout"],
            "hook": True, "prompt": "", "start_time": None, "end_time": None,
            "aspect": free["aspect"], "trim_pauses": True,
        })
    return req, plan_key, max_minutes


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
def create_job(req: JobRequest, user: dict = Depends(accounts.current_user)) -> dict:
    if not _YT.match(req.url.strip()):
        raise HTTPException(400, "That doesn't look like a YouTube link.")
    _check_options(req)
    req, plan_key, _ = _plan_request(req, user)
    reservation = accounts.reserve_quota(user, req.clip_count)
    if reservation["plan"] != plan_key:
        accounts.refund_quota(
            {"owner_id": str(user["_id"]), "plan": reservation["plan"],
             "quota_period": datetime.now(timezone.utc).strftime("%Y-%m")},
            req.clip_count,
        )
        raise HTTPException(409, "Your plan changed. Reload the page and try again.")
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    job_id = uuid.uuid4().hex[:10]
    try:
        return _new_job(req, job_id, owner_id=str(user["_id"]), plan=plan_key, quota_period=period,
                        quota_reserved=req.clip_count)
    except Exception:
        _jobs.pop(job_id, None)
        if config.MONGODB_URI and str(user["_id"]) != "test-user":
            accounts.job_store().delete_one({"id": job_id, "owner_id": str(user["_id"])})
        shutil.rmtree(_job_dir(job_id), ignore_errors=True)
        accounts.refund_quota(
            {"owner_id": str(user["_id"]), "plan": plan_key, "quota_period": period}, req.clip_count
        )
        raise


_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
_MAX_UPLOAD_BYTES = 500 * 1024**2


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
    user: dict = Depends(accounts.current_user),
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
    req, plan_key, _ = _plan_request(req, user)
    job_id = uuid.uuid4().hex[:10]
    d = _job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with open(d / f"source{ext}", "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"File is too large (limit {_MAX_UPLOAD_BYTES // 1024**2} MiB).")
                out.write(chunk)
    except BaseException:  # includes a cancelled/interrupted upload
        shutil.rmtree(d, ignore_errors=True)
        raise
    try:
        reservation = accounts.reserve_quota(user, req.clip_count)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    if reservation["plan"] != plan_key:
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        accounts.refund_quota(
            {"owner_id": str(user["_id"]), "plan": reservation["plan"], "quota_period": period},
            req.clip_count,
        )
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(409, "Your plan changed. Reload the page and try again.")
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        return _new_job(req, job_id, upload=name, owner_id=str(user["_id"]), plan=plan_key,
                        quota_period=period, quota_reserved=req.clip_count)
    except Exception:
        _jobs.pop(job_id, None)
        if config.MONGODB_URI and str(user["_id"]) != "test-user":
            accounts.job_store().delete_one({"id": job_id, "owner_id": str(user["_id"])})
        shutil.rmtree(d, ignore_errors=True)
        accounts.refund_quota(
            {"owner_id": str(user["_id"]), "plan": plan_key, "quota_period": period}, req.clip_count
        )
        raise


@app.get("/api/jobs/{job_id}/zip")
def download_zip(job_id: str, user: dict = Depends(accounts.current_user)) -> FileResponse:
    with _lock:
        job = _jobs.get(job_id)
        if not job and config.MONGODB_URI:
            job = accounts.job_store().find_one({"id": job_id})
            if job:
                job.pop("_id", None)
                _recover_interrupted_job(job)
                _jobs[job_id] = job
        # Copy the list: a clip can be deleted while the zip is being built.
        clips = list(job.get("clips") or []) if job else []
    if not job:
        raise HTTPException(404, "Job not found.")
    accounts.user_job(user, job)
    d = _job_dir(job_id)
    files = [d / Path(c["video"]).name for c in clips if str(c.get("video", "")).startswith("/media/")]
    files = [f for f in files if f.is_file()]
    if not files and clips and os.environ.get("APP_ENV") == "production":
        import requests

        fd, tmp = tempfile.mkstemp(suffix=".zip", dir=config.DATA_DIR)
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                for i, clip in enumerate(clips, 1):
                    response = requests.get(clip["video"], stream=True, timeout=(10, 90))
                    response.raise_for_status()
                    with tempfile.NamedTemporaryFile(dir=config.DATA_DIR, suffix=".mp4") as item:
                        for chunk in response.iter_content(1024 * 1024):
                            if chunk:
                                item.write(chunk)
                        item.flush()
                        z.write(item.name, f"clip_{i:02d}.mp4")
        except Exception as exc:
            os.unlink(tmp)
            raise HTTPException(502, "Could not retrieve clips from cloud storage.") from exc
        title = re.sub(r'[\\/:*?"<>|]+', "", job.get("title") or "clips").strip()[:80] or "clips"
        return FileResponse(tmp, media_type="application/zip", filename=f"{title}.zip",
                            background=BackgroundTask(os.unlink, tmp))
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


@app.get("/api/jobs/{job_id}/clips/{index}/download")
def download_clip(job_id: str, index: int, user: dict = Depends(accounts.current_user)) -> Response:
    with _lock:
        job = _jobs.get(job_id)
        if not job and config.MONGODB_URI:
            job = accounts.job_store().find_one({"id": job_id})
            if job:
                job.pop("_id", None)
                _recover_interrupted_job(job)
                _jobs[job_id] = job
        if not job:
            raise HTTPException(404, "Video job not found.")
        accounts.user_job(user, job)
        clips = job.get("clips") or []
        if not 0 <= index < len(clips):
            raise HTTPException(404, "Clip not found.")
        clip = clips[index]
        video_url = str(clip.get("video") or "")
        filename = re.sub(r'[\\/:*?"<>|]+', "", str(clip.get("title") or "clip")).strip()[:80] or "clip"
        filename += ".mp4"
    if video_url.startswith("/media/"):
        path = _job_dir(job_id) / Path(video_url).name
        if not path.is_file():
            raise HTTPException(404, "Clip media not found.")
        return FileResponse(path, media_type="video/mp4", filename=filename)
    if not video_url.startswith("https://res.cloudinary.com/"):
        raise HTTPException(404, "Clip media not found.")
    import requests

    remote = None
    try:
        remote = requests.get(video_url, stream=True, timeout=(10, 90))
        remote.raise_for_status()
    except requests.RequestException as exc:
        if remote is not None:
            remote.close()
        raise HTTPException(502, "Could not retrieve this clip from cloud storage.") from exc

    def content():
        try:
            yield from remote.iter_content(chunk_size=1024 * 1024)
        finally:
            remote.close()

    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"}
    if remote.headers.get("content-length"):
        headers["Content-Length"] = remote.headers["content-length"]
    return StreamingResponse(content(), media_type="video/mp4", headers=headers)


@app.get("/api/jobs")
def list_jobs(user: dict = Depends(accounts.current_user)) -> list[dict]:
    if config.MONGODB_URI and str(user["_id"]) != "test-user":
        saved = list(accounts.job_store().find(
            {} if user.get("role") == "admin" else {"owner_id": str(user["_id"])}
        ))
        with _lock:
            for job in saved:
                job_id = job["id"]
                if job_id in _jobs:
                    continue
                job.pop("_id", None)
                _recover_interrupted_job(job)
                _jobs[job_id] = job
    visible = [j for j in _jobs.values() if user.get("role") == "admin" or j.get("owner_id") == str(user["_id"])]
    return sorted(visible, key=lambda j: -j["created"])


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: dict = Depends(accounts.current_user)) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if not job and config.MONGODB_URI:
            job = accounts.job_store().find_one({"id": job_id})
            if job:
                job.pop("_id", None)
                _recover_interrupted_job(job)
                _jobs[job_id] = job
    if not job:
        raise HTTPException(404, "Job not found.")
    accounts.user_job(user, job)
    return job


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, user: dict = Depends(accounts.current_user)) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        accounts.user_job(user, job)
        if job["status"] in ("queued", "running"):
            raise HTTPException(409, "This video is still processing.")
        clips = list(job.get("clips") or [])
    _delete_cloudinary_assets(clips)
    with _lock:
        if _jobs.get(job_id) is not job:
            raise HTTPException(409, "This video changed while it was being deleted. Please retry.")
        del _jobs[job_id]
        if config.MONGODB_URI and str(user["_id"]) != "test-user":
            accounts.job_store().delete_one({"id": job_id, "owner_id": str(user["_id"])})
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}/clips/{index}")
def delete_clip(job_id: str, index: int, user: dict = Depends(accounts.current_user)) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        accounts.user_job(user, job)
        clips = job.get("clips") or []
        if not 0 <= index < len(clips):
            raise HTTPException(404, "Clip not found.")
        if job["status"] in ("queued", "running"):
            raise HTTPException(409, "This video is still processing.")
        clip = clips[index]
    _delete_cloudinary_assets([clip])
    with _lock:
        current = _jobs.get(job_id)
        if current is not job or index >= len(job.get("clips") or []) or job["clips"][index].get("video") != clip.get("video"):
            raise HTTPException(409, "This clip changed while it was being deleted. Please retry.")
        clips = job.get("clips") or []
        clips.pop(index)
        _save(job)
    d = _job_dir(job_id)
    for url in (clip.get("video"), clip.get("thumb")):
        if url and str(url).startswith("/media/"):
            (d / Path(url).name).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/media/{job_id}/{filename}")
def media_file(job_id: str, filename: str, user: dict = Depends(accounts.current_user)) -> FileResponse:
    job = _jobs.get(job_id)
    if not job and config.MONGODB_URI:
        job = accounts.job_store().find_one({"id": job_id})
        if job:
            job.pop("_id", None)
            _recover_interrupted_job(job)
            _jobs[job_id] = job
    accounts.user_job(user, job)
    if Path(filename).name != filename:
        raise HTTPException(404, "Media not found.")
    path = _job_dir(job_id) / filename
    if not path.is_file():
        raise HTTPException(404, "Media not found.")
    return FileResponse(path)


app.mount("/fonts", StaticFiles(directory=render.FONTS_DIR), name="fonts")


@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/admin")
@app.get("/admin/login")
def admin_page() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "admin.html")
