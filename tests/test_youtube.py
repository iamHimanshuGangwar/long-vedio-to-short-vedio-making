"""YouTube publishing: metadata rules, Google sign-in and resumable upload.
Google is never contacted: the OAuth flow and the API client are faked."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app import youtube

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


# ---------- metadata (rules from the videos resource reference) ----------

def test_title_max_100_chars_and_no_angle_brackets():
    meta = youtube.build_metadata(title="<b>" + "x" * 200 + "</b>", description="", now=NOW)
    title = meta["snippet"]["title"]
    assert len(title) <= 100 and "<" not in title and ">" not in title


def test_empty_title_is_rejected():
    with pytest.raises(ValueError, match="title"):
        youtube.build_metadata(title="  <> ", description="", now=NOW)


def test_description_max_5000_bytes_without_breaking_characters():
    meta = youtube.build_metadata(title="t", description="é" * 4000, now=NOW)  # 8000 bytes
    desc = meta["snippet"]["description"]
    assert len(desc.encode("utf-8")) <= 5000
    desc.encode("utf-8").decode("utf-8")  # still valid UTF-8


def test_hashtags_go_into_description_and_tags_within_500_chars():
    tags = [f"tag{i}" for i in range(200)]
    meta = youtube.build_metadata(title="t", description="Watch this", hashtags=["#shorts", "funny"] + tags, now=NOW)
    assert "#shorts #funny" in meta["snippet"]["description"]
    assert meta["snippet"]["tags"][:2] == ["shorts", "funny"]
    assert len(",".join(meta["snippet"]["tags"])) <= 500


def test_tags_with_spaces_count_their_quotes():
    meta = youtube.build_metadata(title="t", description="", hashtags=["a b" * 1] * 200, now=NOW)
    cost = sum(len(t) + (2 if " " in t else 0) for t in meta["snippet"]["tags"]) + len(meta["snippet"]["tags"]) - 1
    assert cost <= 500


def test_publish_now_defaults():
    meta = youtube.build_metadata(title="t", description="", now=NOW)
    assert meta["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False}


@pytest.mark.parametrize("privacy", ["public", "unlisted", "private"])
def test_privacy_values(privacy):
    assert youtube.build_metadata(title="t", description="", privacy=privacy, now=NOW)["status"]["privacyStatus"] == privacy


def test_unknown_privacy_rejected():
    with pytest.raises(ValueError, match="privacy"):
        youtube.build_metadata(title="t", description="", privacy="friends", now=NOW)


def test_schedule_forces_private_and_utc_iso_time():
    when = datetime(2026, 11, 16, 18, 30, tzinfo=timezone(timedelta(hours=5)))
    meta = youtube.build_metadata(title="t", description="", privacy="public", publish_at=when, now=NOW)
    # publishAt "can be set only if the privacy status of the video is private"
    assert meta["status"]["privacyStatus"] == "private"
    assert meta["status"]["publishAt"] == "2026-11-16T13:30:00Z"


def test_schedule_in_the_past_is_rejected():
    with pytest.raises(ValueError, match="future"):
        youtube.build_metadata(title="t", description="", publish_at=NOW - timedelta(minutes=5), now=NOW)


def test_made_for_kids_is_passed_through():
    meta = youtube.build_metadata(title="t", description="", made_for_kids=True, now=NOW)
    assert meta["status"]["selfDeclaredMadeForKids"] is True


def test_full_video_link_is_added():
    meta = youtube.build_metadata(title="t", description="Clip", hashtags=["fun"],
                                  source_url="https://youtu.be/abc", now=NOW)
    desc = meta["snippet"]["description"]
    assert "Full video: https://youtu.be/abc" in desc
    assert desc.index("Full video") < desc.index("#fun")  # link sits between caption and hashtags


# ---------- configuration and sign-in ----------

@pytest.fixture
def yt_env(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(youtube, "TOKEN_PATH", tmp_path / "youtube_token.json")
    monkeypatch.setattr(youtube, "_pending", {})
    return tmp_path


def test_not_configured_without_client(monkeypatch, tmp_path):
    monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(youtube, "TOKEN_PATH", tmp_path / "t.json")
    assert youtube.status() == {"configured": False, "connected": False, "channel": None}
    with pytest.raises(youtube.NotConfigured):
        youtube.start_auth("http://127.0.0.1:8765/")


def test_auth_url_asks_for_offline_access_with_pkce(yt_env):
    url = youtube.start_auth("http://127.0.0.1:8765/")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://accounts.google.com/")
    assert q["client_id"] == ["cid.apps.googleusercontent.com"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8765/"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0]
    assert set(q["scope"][0].split()) == set(youtube.SCOPES)
    assert q["state"][0] in youtube._pending


def test_finish_auth_rejects_unknown_state(yt_env):
    with pytest.raises(youtube.AuthError):
        youtube.finish_auth("forged-state", "code")


def test_finish_auth_saves_credentials_and_channel(yt_env, monkeypatch):
    url = youtube.start_auth("http://127.0.0.1:8765/")
    state = parse_qs(urlparse(url).query)["state"][0]
    flow = youtube._pending[state]["flow"]

    class Creds:
        def to_json(self):
            return json.dumps({"token": "t", "refresh_token": "r"})

    monkeypatch.setattr(flow, "fetch_token", lambda **kw: None)
    monkeypatch.setattr(type(flow), "credentials", property(lambda self: Creds()))
    monkeypatch.setattr(youtube, "_fetch_channel", lambda creds: {"id": "UC1", "title": "My Channel"})
    channel = youtube.finish_auth(state, "the-code")
    assert channel["title"] == "My Channel"
    saved = json.loads(youtube.TOKEN_PATH.read_text())
    assert saved["refresh_token"] == "r" and saved["channel"]["id"] == "UC1"
    assert state not in youtube._pending  # one-time use
    assert youtube.status()["connected"] is True


def test_disconnect_forgets_the_token(yt_env, monkeypatch):
    youtube.TOKEN_PATH.write_text(json.dumps({"token": "t", "refresh_token": "r", "channel": {"title": "x"}}))
    revoked = []
    monkeypatch.setattr(youtube, "_revoke", lambda token: revoked.append(token))
    youtube.disconnect()
    assert not youtube.TOKEN_PATH.exists() and revoked == ["r"]


# ---------- upload ----------

class FakeRequest:
    def __init__(self, steps):
        self.steps = list(steps)

    def next_chunk(self):
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class FakeVideos:
    def __init__(self, request):
        self.request, self.kwargs = request, None

    def insert(self, **kwargs):
        self.kwargs = kwargs
        return self.request


class FakeService:
    def __init__(self, request):
        self._videos = FakeVideos(request)

    def videos(self):
        return self._videos


class Status:
    def __init__(self, p):
        self.p = p

    def progress(self):
        return self.p


def _http_error(code, reason):
    body = json.dumps({"error": {"code": code, "errors": [{"reason": reason, "message": reason}]}}).encode()
    return HttpError(httplib2.Response({"status": code}), body)


def _clip(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\0" * 1024)
    return f


def test_upload_reports_progress_and_returns_video(tmp_path, monkeypatch):
    req = FakeRequest([(Status(0.5), None), (None, {"id": "vid123"})])
    svc = FakeService(req)
    seen = []
    meta = youtube.build_metadata(title="t", description="d", now=NOW)
    vid = youtube.upload(_clip(tmp_path), meta, notify_subscribers=False, progress=seen.append, service=svc,
                         sleep=lambda s: None)
    assert vid == "vid123"
    assert seen[0] == 50
    kw = svc._videos.kwargs
    assert kw["part"] == "snippet,status" and kw["body"] == meta and kw["notifySubscribers"] is False
    assert kw["media_body"].resumable()


def test_upload_retries_server_errors(tmp_path):
    req = FakeRequest([_http_error(503, "backendError"), (None, {"id": "v"})])
    slept = []
    assert youtube.upload(_clip(tmp_path), {}, service=FakeService(req), sleep=slept.append) == "v"
    assert len(slept) == 1


def test_upload_gives_up_after_max_retries(tmp_path):
    req = FakeRequest([_http_error(500, "backendError")] * 20)
    with pytest.raises(youtube.UploadError):
        youtube.upload(_clip(tmp_path), {}, service=FakeService(req), sleep=lambda s: None)


@pytest.mark.parametrize("code, reason, words", [
    (400, "uploadLimitExceeded", "upload limit"),
    (403, "quotaExceeded", "100 uploads per day"),
    (400, "invalidPublishAt", "schedule"),
    (401, "authError", "Reconnect"),
])
def test_upload_errors_are_explained(tmp_path, code, reason, words):
    req = FakeRequest([_http_error(code, reason)])
    with pytest.raises(youtube.UploadError, match=words):
        youtube.upload(_clip(tmp_path), {}, service=FakeService(req), sleep=lambda s: None)


# ---------- review fixes ----------

def test_scheduled_video_must_be_public():
    """publishAt is the time YouTube makes the video public, so a schedule with
    unlisted/private would silently become public: refuse it."""
    for privacy in ("unlisted", "private"):
        with pytest.raises(ValueError, match="public"):
            youtube.build_metadata(title="t", description="", privacy=privacy,
                                   publish_at=NOW + timedelta(days=1), now=NOW)


def test_long_description_keeps_the_link_and_hashtags():
    meta = youtube.build_metadata(title="t", description="é" * 4000, hashtags=["fun", "shorts"],
                                  source_url="https://youtu.be/abc", now=NOW)
    desc = meta["snippet"]["description"]
    assert len(desc.encode("utf-8")) <= 5000
    assert "Full video: https://youtu.be/abc" in desc and desc.endswith("#fun #shorts")


def test_token_refresh_is_saved_atomically(yt_env, monkeypatch):
    youtube.TOKEN_PATH.write_text(json.dumps({"token": "old", "refresh_token": "r", "channel": {"title": "C"}}))
    youtube._save_token({"token": "new", "refresh_token": "r"}, channel={"title": "C"})
    saved = json.loads(youtube.TOKEN_PATH.read_text())
    assert saved["token"] == "new" and saved["channel"] == {"title": "C"}
    assert not list(youtube.TOKEN_PATH.parent.glob("*.tmp"))
