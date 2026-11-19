"""API endpoints for connecting YouTube and posting clips."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main, youtube


@pytest.fixture
def client(monkeypatch):
    queued = []
    monkeypatch.setattr(main._pool, "submit", lambda *a: None)
    monkeypatch.setattr(main._upload_pool, "submit", lambda fn, *a: queued.append(a))
    monkeypatch.setattr(main, "_jobs", {})
    c = TestClient(main.app, follow_redirects=False)
    c.queued = queued
    return c


def _done_job(job_id="0000000001"):
    job = {
        "id": job_id, "url": "https://youtu.be/abc", "status": "done", "progress": 100, "message": "Done",
        "title": "Big video", "created": 1.0, "error": None, "warning": None, "options": {},
        "clips": [{"title": "Clip one", "description": "A great moment", "hashtags": ["fun"],
                   "video": f"/media/{job_id}/clip_01.mp4", "thumb": "", "length": 40}],
    }
    main._jobs[job_id] = job
    d = main._job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "clip_01.mp4").write_bytes(b"\0" * 100)
    return job


def _connected(monkeypatch, yes=True):
    monkeypatch.setattr(youtube, "status", lambda: {"configured": True, "connected": yes,
                                                    "channel": {"title": "My Channel"} if yes else None})


def test_status_endpoint(client, monkeypatch):
    _connected(monkeypatch)
    assert client.get("/api/youtube").json()["channel"]["title"] == "My Channel"


def test_connect_redirects_to_google(client, monkeypatch):
    monkeypatch.setattr(youtube, "start_auth", lambda redirect: f"https://accounts.google.com/x?r={redirect}")
    res = client.get("/api/youtube/connect")
    assert res.status_code == 307
    assert res.headers["location"].startswith("https://accounts.google.com/")
    assert "r=http://testserver/" in res.headers["location"]


def test_connect_without_setup_explains(client, monkeypatch):
    def boom(_):
        raise youtube.NotConfigured("set YOUTUBE_CLIENT_ID")
    monkeypatch.setattr(youtube, "start_auth", boom)
    res = client.get("/api/youtube/connect")
    assert res.status_code == 400 and "YOUTUBE_CLIENT_ID" in res.json()["detail"]


def test_google_redirect_back_finishes_sign_in(client, monkeypatch):
    got = []
    monkeypatch.setattr(youtube, "finish_auth", lambda state, code: got.append((state, code)) or {"title": "C"})
    res = client.get("/?state=s1&code=c1")
    assert got == [("s1", "c1")]
    assert res.status_code == 303 and res.headers["location"] == "/?youtube=connected"


def test_google_redirect_back_with_error(client, monkeypatch):
    res = client.get("/?error=access_denied&state=s1")
    assert res.status_code == 303 and "youtube=error" in res.headers["location"]


def test_disconnect(client, monkeypatch):
    calls = []
    monkeypatch.setattr(youtube, "disconnect", lambda: calls.append(1))
    assert client.post("/api/youtube/disconnect").json() == {"ok": True} and calls


def test_publish_requires_connection(client, monkeypatch):
    _connected(monkeypatch, yes=False)
    _done_job()
    res = client.post("/api/jobs/0000000001/clips/0/youtube", json={"title": "t"})
    assert res.status_code == 409


def test_publish_queues_upload_and_marks_clip(client, monkeypatch):
    _connected(monkeypatch)
    _done_job()
    res = client.post("/api/jobs/0000000001/clips/0/youtube",
                      json={"title": "My title", "description": "d", "hashtags": ["fun"], "privacy": "unlisted",
                            "made_for_kids": False, "notify_subscribers": True, "add_source_link": True})
    assert res.status_code == 200
    clip = main._jobs["0000000001"]["clips"][0]
    assert clip["youtube"]["status"] == "queued"
    (job_id, index, meta, notify), = client.queued
    assert (job_id, index, notify) == ("0000000001", 0, True)
    assert meta["snippet"]["title"] == "My title" and meta["status"]["privacyStatus"] == "unlisted"
    assert "Full video: https://youtu.be/abc" in meta["snippet"]["description"]


def test_publish_schedule(client, monkeypatch):
    _connected(monkeypatch)
    _done_job()
    when = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    res = client.post("/api/jobs/0000000001/clips/0/youtube", json={"title": "t", "publish_at": when.isoformat()})
    assert res.status_code == 200
    (_, _, meta, _), = client.queued
    assert meta["status"]["privacyStatus"] == "private" and meta["status"]["publishAt"].endswith("Z")


@pytest.mark.parametrize("body, code", [
    ({"title": "t", "publish_at": "2000-01-01T00:00:00Z"}, 400),  # past
    ({"title": "t", "privacy": "friends"}, 400),
    ({"title": "<>"}, 400),
    ({"title": "t", "publish_at": "2030-01-01T00:00:00"}, 400),  # no timezone
])
def test_publish_rejects_bad_input(client, monkeypatch, body, code):
    _connected(monkeypatch)
    _done_job()
    assert client.post("/api/jobs/0000000001/clips/0/youtube", json=body).status_code == code
    assert client.queued == []


def test_publish_unknown_clip(client, monkeypatch):
    _connected(monkeypatch)
    _done_job()
    assert client.post("/api/jobs/0000000001/clips/5/youtube", json={"title": "t"}).status_code == 404
    assert client.post("/api/jobs/0000000099/clips/0/youtube", json={"title": "t"}).status_code == 404


def test_cannot_post_the_same_clip_twice_at_once(client, monkeypatch):
    _connected(monkeypatch)
    _done_job()
    assert client.post("/api/jobs/0000000001/clips/0/youtube", json={"title": "t"}).status_code == 200
    assert client.post("/api/jobs/0000000001/clips/0/youtube", json={"title": "t"}).status_code == 409


def test_upload_worker_records_result(client, monkeypatch):
    _done_job()
    main._jobs["0000000001"]["clips"][0]["youtube"] = {"status": "queued"}
    monkeypatch.setattr(youtube, "upload", lambda path, meta, notify_subscribers, progress: progress(100) or "vid9")
    main._publish("0000000001", 0, {"status": {"privacyStatus": "public"}}, True)
    yt = main._jobs["0000000001"]["clips"][0]["youtube"]
    assert yt["status"] == "done" and yt["url"] == "https://youtu.be/vid9" and yt["video_id"] == "vid9"


def test_upload_worker_records_error(client, monkeypatch):
    _done_job()
    main._jobs["0000000001"]["clips"][0]["youtube"] = {"status": "queued"}

    def fail(*a, **k):
        raise youtube.UploadError("YouTube's daily limit of 100 uploads per day was reached.")
    monkeypatch.setattr(youtube, "upload", fail)
    main._publish("0000000001", 0, {}, True)
    yt = main._jobs["0000000001"]["clips"][0]["youtube"]
    assert yt["status"] == "error" and "100 uploads" in yt["error"]


def test_interrupted_upload_is_marked_after_restart(tmp_path):
    job = _done_job("0000000002")
    job["clips"][0]["youtube"] = {"status": "uploading", "progress": 40}
    main._save(job)
    main._jobs.clear()
    main._load_existing()
    yt = main._jobs["0000000002"]["clips"][0]["youtube"]
    assert yt["status"] == "error" and "closed" in yt["error"]


# ---------- review fixes ----------

def test_cannot_remove_a_video_while_a_clip_is_posting(client, monkeypatch):
    _connected(monkeypatch)
    _done_job()
    client.post("/api/jobs/0000000001/clips/0/youtube", json={"title": "t"})
    res = client.delete("/api/jobs/0000000001")
    assert res.status_code == 409 and "YouTube" in res.json()["detail"]
    assert "0000000001" in main._jobs


def test_schedule_with_unlisted_is_rejected(client, monkeypatch):
    _connected(monkeypatch)
    _done_job()
    when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    res = client.post("/api/jobs/0000000001/clips/0/youtube", json={"title": "t", "privacy": "unlisted", "publish_at": when})
    assert res.status_code == 400


@pytest.mark.parametrize("method, path", [
    ("post", "/api/youtube/disconnect"),
    ("delete", "/api/jobs/0000000001"),
    ("post", "/api/jobs/0000000001/clips/0/youtube"),
    ("post", "/api/jobs"),
])
def test_requests_from_other_websites_are_refused(client, monkeypatch, method, path):
    """A page on another site can't make the browser change anything here (CSRF)."""
    _connected(monkeypatch)
    _done_job()
    disconnected = []
    monkeypatch.setattr(youtube, "disconnect", lambda: disconnected.append(1))
    res = getattr(client, method)(path, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    assert disconnected == [] and "0000000001" in main._jobs


def test_same_origin_requests_still_work(client, monkeypatch):
    calls = []
    monkeypatch.setattr(youtube, "disconnect", lambda: calls.append(1))
    res = client.post("/api/youtube/disconnect", headers={"Origin": "http://testserver"})
    assert res.status_code == 200 and calls


def test_sign_in_error_redirect_uses_a_fixed_reason(client):
    res = client.get("/?error=//evil.example/%0Ax&state=s1")
    assert res.status_code == 303
    assert res.headers["location"] in ("/?youtube=error&reason=failed", "/?youtube=error&reason=access_denied")
    res = client.get("/?error=access_denied&state=s1")
    assert res.headers["location"] == "/?youtube=error&reason=access_denied"


def test_post_is_refused_if_the_clip_changed_since_the_dialog_opened(client, monkeypatch):
    # another tab deleted an earlier clip, so this index now holds a different clip
    _connected(monkeypatch)
    _done_job()
    res = client.post("/api/jobs/0000000001/clips/0/youtube",
                      json={"title": "T", "clip_video": "/media/0000000001/clip_02.mp4"})
    assert res.status_code == 409 and "changed" in res.json()["detail"]
    assert client.queued == []


def test_post_goes_ahead_when_the_clip_matches(client, monkeypatch):
    _connected(monkeypatch)
    _done_job()
    res = client.post("/api/jobs/0000000001/clips/0/youtube",
                      json={"title": "T", "clip_video": "/media/0000000001/clip_01.mp4"})
    assert res.status_code == 200 and len(client.queued) == 1
