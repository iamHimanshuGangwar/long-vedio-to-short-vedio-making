"""Post clips to the user's own YouTube channel.

Uses the YouTube Data API v3 with the user's own Google "Desktop app" OAuth
client (see docs/youtube-setup.md). Limits and rules below come from Google's
reference docs:
  - videos.insert has its own bucket: 100 uploads per day per project.
  - title: max 100 characters; title and description: no '<' or '>'.
  - description: max 5000 bytes; tags: max 500 characters in total.
  - publishAt: only with privacyStatus "private"; YouTube publishes it itself.
  - Desktop apps are public clients: use PKCE and a loopback redirect
    (http://127.0.0.1:port, documented without a path).
"""
from __future__ import annotations

import json
import os
import random
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # to show which channel is connected
]
TOKEN_PATH = config.DATA_DIR / "youtube_token.json"
PRIVACY = ("public", "unlisted", "private")

# Google may return the scopes in a different order or with previously granted
# ones added; without this oauthlib treats that as an error.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

_pending: dict[str, dict] = {}  # OAuth state -> {"flow", "created"}; sign-ins in progress
_PENDING_TTL = 15 * 60


class NotConfigured(RuntimeError):
    pass


class AuthError(RuntimeError):
    pass


class UploadError(RuntimeError):
    pass


# ---------- metadata ----------

def _clean(text: str) -> str:
    return (text or "").replace("<", "").replace(">", "").strip()


def _cut_bytes(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore").rstrip()


def _tags(hashtags: list[str]) -> list[str]:
    """Tags within YouTube's 500-character total (commas count; a tag with a
    space counts its surrounding quotes)."""
    out, used = [], 0
    for tag in hashtags:
        tag = _clean(tag.lstrip("#"))
        if not tag or tag in out:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if out else 0)
        if used + cost > 500:
            break
        out.append(tag)
        used += cost
    return out


def build_metadata(
    title: str,
    description: str,
    hashtags: list[str] | None = None,
    privacy: str = "public",
    publish_at: datetime | None = None,
    made_for_kids: bool = False,
    source_url: str | None = None,
    now: datetime | None = None,
) -> dict:
    """The request body for videos.insert. Raises ValueError with a message
    that can be shown to the user."""
    if privacy not in PRIVACY:
        raise ValueError("Unknown privacy setting.")
    title = _clean(title)[:100].strip()
    if not title:
        raise ValueError("The title can't be empty.")

    tags = _tags(hashtags or [])
    tail = []
    if source_url:
        tail.append(f"Full video: {source_url}")  # between the caption and the hashtags
    if tags:
        tail.append(" ".join("#" + t.replace(" ", "") for t in tags))
    tail_text = "\n\n".join(tail)
    # Trim the caption, not the link or hashtags, to fit YouTube's 5000-byte limit.
    room = 5000 - len(tail_text.encode("utf-8")) - (2 if tail_text else 0)
    caption = _cut_bytes(_clean(description), max(0, room))
    desc = _cut_bytes("\n\n".join(p for p in (caption, tail_text) if p), 5000)

    status: dict = {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(made_for_kids)}
    if publish_at is not None:
        # publishAt is the moment YouTube makes the video public, so a scheduled
        # unlisted/private post would quietly go public: refuse instead.
        if privacy != "public":
            raise ValueError("Scheduled videos go public at the chosen time. Choose Public, "
                             "or post now as unlisted or private.")
        if publish_at.tzinfo is None:
            raise ValueError("The schedule time needs a time zone.")
        now = now or datetime.now(timezone.utc)
        if publish_at <= now:
            raise ValueError("The schedule time must be in the future.")
        status["privacyStatus"] = "private"  # required by YouTube for publishAt
        status["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    snippet = {"title": title, "description": desc}
    if tags:
        snippet["tags"] = tags
    return {"snippet": snippet, "status": status}


# ---------- sign-in ----------

def _client_config() -> dict | None:
    cid, secret = os.environ.get("YOUTUBE_CLIENT_ID", "").strip(), os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        return None
    return {"installed": {
        "client_id": cid,
        "client_secret": secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://127.0.0.1"],
    }}


def _read_token() -> dict | None:
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status() -> dict:
    token = _read_token()
    return {
        "configured": _client_config() is not None,
        "connected": bool(token and token.get("refresh_token")),
        "channel": (token or {}).get("channel"),
    }


def start_auth(redirect_uri: str) -> str:
    """Start Google sign-in; returns the URL to send the browser to."""
    from google_auth_oauthlib.flow import Flow

    cfg = _client_config()
    if cfg is None:
        raise NotConfigured(
            "YouTube isn't set up yet: add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET to .env "
            "(see docs/youtube-setup.md) and restart the app."
        )
    cutoff = time.time() - _PENDING_TTL
    for s in [s for s, p in _pending.items() if p["created"] < cutoff]:
        del _pending[s]
    flow = Flow.from_client_config(cfg, scopes=SCOPES, redirect_uri=redirect_uri)  # PKCE S256 by default
    state = secrets.token_urlsafe(24)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    _pending[state] = {"flow": flow, "created": time.time()}
    return url


def _fetch_channel(creds) -> dict | None:
    from googleapiclient.discovery import build

    svc = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = svc.channels().list(part="snippet", mine=True).execute().get("items") or []
    if not items:
        return None
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def finish_auth(state: str, code: str) -> dict | None:
    """Google redirected back: swap the code for tokens and save them."""
    pending = _pending.pop(state, None)
    if pending is None or pending["created"] < time.time() - _PENDING_TTL:
        raise AuthError("This sign-in link has expired. Click Connect YouTube again.")
    flow = pending["flow"]
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # oauthlib raises several types
        raise AuthError(f"Google sign-in failed: {exc}") from exc
    creds = flow.credentials
    try:
        channel = _fetch_channel(creds)
    except Exception:
        channel = None  # still connected; the channel name is only for display
    _save_token(json.loads(creds.to_json()), channel)
    return channel


def _save_token(data: dict, channel: dict | None) -> None:
    """Write the login atomically, so a crash mid-write can't corrupt it."""
    data = dict(data, channel=channel)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(TOKEN_PATH)


def _revoke(token: str) -> None:
    import requests

    requests.post("https://oauth2.googleapis.com/revoke", params={"token": token}, timeout=10)


def disconnect() -> None:
    token = _read_token()
    if token:
        try:
            _revoke(token.get("refresh_token") or token.get("token") or "")
        except Exception:
            pass  # forget it locally even if Google can't be reached
    TOKEN_PATH.unlink(missing_ok=True)


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token = _read_token()
    if not token or not token.get("refresh_token"):
        raise UploadError("YouTube isn't connected. Click Connect YouTube first.")
    creds = Credentials.from_authorized_user_info(token, SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise UploadError(
                "YouTube sign-in has expired or was revoked. Reconnect YouTube (click Connect YouTube at the top). "
                "Google signs Testing-mode apps out every 7 days; see docs/youtube-setup.md."
            ) from exc
        _save_token(json.loads(creds.to_json()), token.get("channel"))
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# ---------- upload ----------

MAX_RETRIES = 10
_RETRY_STATUS = (500, 502, 503, 504)
_CHUNK = 8 * 1024 * 1024  # multiple of 256 KB, as resumable uploads require

_REASONS = {
    "uploadLimitExceeded": "Your YouTube channel has hit its upload limit for now. Try again later.",
    "quotaExceeded": "YouTube's daily limit of 100 uploads per day for your Google app was reached. "
                     "Try again tomorrow.",
    "invalidPublishAt": "YouTube rejected the schedule time. Pick a time further in the future.",
    "invalidTitle": "YouTube rejected the title.",
    "invalidDescription": "YouTube rejected the description.",
    "invalidTags": "YouTube rejected the hashtags.",
    "forbiddenPrivacySetting": "YouTube doesn't allow that privacy setting for this channel.",
}


def _explain(err) -> str:
    try:
        body = json.loads(err.content.decode("utf-8"))["error"]
        reason = (body.get("errors") or [{}])[0].get("reason", "")
        message = body.get("message", "")
    except Exception:
        reason, message = "", str(err)
    if reason in _REASONS:
        return _REASONS[reason]
    if err.resp.status == 401:
        return "YouTube sign-in has expired or was revoked. Reconnect YouTube."
    return f"YouTube refused the upload ({err.resp.status}): {message or reason}"


def upload(
    path: Path,
    metadata: dict,
    notify_subscribers: bool = True,
    progress: Callable[[int], None] | None = None,
    service=None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Resumable upload with exponential backoff, following Google's sample:
    retry 500/502/503/504 and network errors, fail fast on anything else."""
    import http.client

    import httplib2
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    service = service or _service()
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=_CHUNK, resumable=True)
    request = service.videos().insert(
        part="snippet,status", body=metadata, media_body=media, notifySubscribers=notify_subscribers
    )
    retriable = (httplib2.HttpLib2Error, OSError, http.client.NotConnected, http.client.IncompleteRead,
                 http.client.ImproperConnectionState, http.client.CannotSendRequest,
                 http.client.CannotSendHeader, http.client.ResponseNotReady, http.client.BadStatusLine)
    response, retry = None, 0
    while response is None:
        try:
            status_, response = request.next_chunk()
            if status_ is not None and progress:
                progress(int(status_.progress() * 100))
            continue
        except HttpError as err:
            if err.resp.status not in _RETRY_STATUS:
                raise UploadError(_explain(err)) from err
        except retriable:
            pass
        retry += 1
        if retry > MAX_RETRIES:
            raise UploadError("The upload kept failing. Check your internet connection and try again.")
        sleep(random.random() * 2 ** retry)
    if "id" not in response:
        raise UploadError(f"YouTube didn't confirm the upload: {response}")
    if progress:
        progress(100)
    return response["id"]
