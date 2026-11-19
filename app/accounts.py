"""Authentication, plans, quotas, and MongoDB persistence."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from fastapi import HTTPException, Request
from pymongo import MongoClient, ReturnDocument

from . import config

SESSION_COOKIE = "reelcraft_session"
PLANS = {
    "free": {"name": "Free", "monthly": 0, "yearly": 0, "quota": 1, "period": "month", "minutes": 10},
    "normal": {"name": "Normal", "monthly": 49, "yearly": 449, "quota": 32, "period": "month", "minutes": 30},
    "premium": {"name": "Premium", "monthly": 99, "yearly": 899, "quota": 90, "period": "month", "minutes": 90},
    "gold": {"name": "Gold", "monthly": 149, "yearly": 1399, "quota": None, "period": "month", "minutes": 300},
}
FREE_SETTINGS = {"clip_count": 1, "min_len": 15, "max_len": 30, "aspect": "9:16", "layout": "auto"}


@lru_cache(maxsize=1)
def mongo_db():
    if not config.MONGODB_URI:
        raise HTTPException(503, "User accounts are not configured. Set MONGODB_URI in the deployment environment.")
    client = MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, tz_aware=True)
    try:
        client.admin.command("ping")
        db = client[config.MONGODB_DATABASE]
        db.users.create_index("email", unique=True)
        db.jobs.create_index("id", unique=True)
        db.jobs.create_index("owner_id")
    except Exception as exc:
        client.close()
        raise HTTPException(503, "The account database is unavailable.") from exc
    return client[config.MONGODB_DATABASE]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _session_secret() -> bytes:
    secret = config.SESSION_SECRET
    if len(secret) < 32:
        raise HTTPException(503, "Set SESSION_SECRET to a random value at least 32 characters long.")
    return secret.encode()


def make_session(user_id: str) -> str:
    payload = _b64(json.dumps({"sub": user_id, "exp": int(time.time()) + 60 * 60 * 24 * 14}).encode())
    sig = _b64(hmac.new(_session_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_session(token: str) -> str | None:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64(hmac.new(_session_secret(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(data["sub"]) if int(data["exp"]) > time.time() else None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    plan_key = user.get("plan", "free")
    plan = PLANS.get(plan_key, PLANS["free"])
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    used = int(user.get("used", 0)) if user.get("cycle") == month else 0
    return {
        "id": str(user["_id"]), "email": user["email"], "name": user.get("name", ""),
        "picture": user.get("picture", ""), "role": user.get("role", "user"), "plan": plan_key,
        "plan_name": plan["name"], "billing": user.get("billing", "monthly"),
        "used": used, "quota": plan["quota"],
        "cycle": user.get("cycle", ""), "banned": bool(user.get("banned", False)),
    }


def google_login(credential: str) -> dict[str, Any]:
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google sign-in is not configured.")
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(credential, GoogleRequest(), config.GOOGLE_CLIENT_ID)
    except Exception as exc:
        raise HTTPException(401, "Google sign-in could not be verified. Please try again.") from exc
    email = str(claims.get("email", "")).strip().lower()
    if not email or claims.get("email_verified") is not True:
        raise HTTPException(403, "Use a Google account with a verified email address.")
    blocked = {d.strip().lower() for d in config.BLOCKED_EMAIL_DOMAINS.split(",") if d.strip()}
    if email.rsplit("@", 1)[-1] in blocked:
        raise HTTPException(403, "This email domain is not allowed.")
    if email == config.ADMIN_EMAIL:
        raise HTTPException(403, "Use the administrator sign-in page for this account.")
    db = mongo_db()
    user = db.users.find_one({"email": email})
    if user and user.get("banned"):
        raise HTTPException(403, "This account has been disabled.")
    if not user:
        user = {
            "email": email, "name": claims.get("name", ""), "picture": claims.get("picture", ""),
            "role": "user",
            "plan": "free", "used": 0, "cycle": "",
            "banned": False, "created_at": datetime.now(timezone.utc),
        }
        db.users.insert_one(user)
    else:
        db.users.update_one({"_id": user["_id"]}, {"$set": {
            "name": claims.get("name", user.get("name", "")), "picture": claims.get("picture", user.get("picture", "")),
        }})
        user.update(name=claims.get("name", user.get("name", "")), picture=claims.get("picture", user.get("picture", "")))
    return user


def current_user(request: Request) -> dict[str, Any]:
    if os.environ.get("REELCRAFT_TEST_MODE") == "1" and os.environ.get("APP_ENV") != "production":
        return {"_id": "test-user", "email": "test@example.com", "role": "admin", "plan": "gold", "used": 0}
    user_id = read_session(request.cookies.get(SESSION_COOKIE, ""))
    if not user_id:
        raise HTTPException(401, "Sign in with Google to continue.")
    user = mongo_db().users.find_one({"_id": _object_id(user_id)})
    if not user:
        raise HTTPException(401, "Your session has expired. Sign in again.")
    if user.get("banned"):
        raise HTTPException(403, "This account has been disabled.")
    return user


def _object_id(value: str):
    from bson import ObjectId

    try:
        return ObjectId(value)
    except Exception:
        raise HTTPException(401, "Invalid account session.") from None


def admin_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(403, "Administrator access required.")
    return user


def reserve_quota(user: dict[str, Any], requested: int) -> dict[str, Any]:
    user_id = user["_id"]
    if str(user_id) == "test-user":
        return _public_user(user)
    key = active_plan(user)
    plan = PLANS.get(key, PLANS["free"])
    if key == "free" and requested != 1:
        raise HTTPException(403, "The Free plan allows one clip per generation.")
    if plan["quota"] is not None and requested > plan["quota"]:
        raise HTTPException(403, f"{plan['name']} allows up to {plan['quota']} clips per monthly cycle.")
    now = datetime.now(timezone.utc)
    cycle = now.strftime("%Y-%m")
    period_field, period_value = "used", cycle
    period_key = "cycle"
    quota = plan["quota"]
    query: dict[str, Any] = {"_id": user_id, "banned": {"$ne": True}}
    if quota is not None:
        query["$or"] = [{period_key: {"$ne": period_value}}, {period_field: {"$lte": quota - requested}}]
    updated = mongo_db().users.find_one_and_update(
        query,
        [{"$set": {
            period_key: period_value,
            period_field: {"$cond": [{"$eq": [f"${period_key}", period_value]}, {"$add": [{"$ifNull": [f"${period_field}", 0]}, requested]}, requested]},
        }}],
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(429, "Your clip allowance for this plan has been used. Upgrade or try again after it resets.")
    return _public_user(updated)


def refund_quota(job: dict[str, Any], count: int) -> None:
    if count <= 0 or not config.MONGODB_URI or job.get("owner_id") == "test-user":
        return
    plan_key = job.get("plan", "free")
    field, period_field, period_value = "used", "cycle", job.get("quota_period")
    if not period_value:
        return
    mongo_db().users.update_one(
        {"_id": _object_id(str(job["owner_id"])), period_field: period_value},
        [{"$set": {field: {"$max": [0, {"$subtract": [{"$ifNull": [f"${field}", 0]}, count]}]}}}],
    )


def job_store():
    return mongo_db().jobs


def persist_job(job: dict[str, Any]) -> None:
    if config.MONGODB_URI and str(job.get("owner_id", "")) != "test-user":
        mongo_db().jobs.replace_one({"id": job["id"]}, job, upsert=True)


def user_job(user: dict[str, Any], job: dict[str, Any] | None) -> dict[str, Any]:
    if not job or (user.get("role") != "admin" and job.get("owner_id") != str(user["_id"])):
        raise HTTPException(404, "Video job not found.")
    return job


def active_plan(user: dict[str, Any]) -> str:
    expires = user.get("plan_expires")
    if user.get("plan") != "free" and isinstance(expires, datetime) and expires <= datetime.now(timezone.utc):
        if str(user.get("_id")) != "test-user":
            mongo_db().users.update_one({"_id": user["_id"]}, {"$set": {"plan": "free", "used": 0, "cycle": ""}})
        user["plan"] = "free"
    return user.get("plan", "free") if user.get("plan") in PLANS else "free"


def server_max_minutes() -> int:
    if not config.MONGODB_URI:
        return config.MAX_SOURCE_MINUTES
    saved = mongo_db().settings.find_one({"_id": "max_source_minutes"})
    return max(1, min(10, int(saved.get("value", config.MAX_SOURCE_MINUTES)))) if saved else config.MAX_SOURCE_MINUTES
