import pytest
from fastapi.testclient import TestClient

from app import accounts, main

YT = "https://www.youtube.com/watch?v=abc123"


@pytest.fixture
def client(monkeypatch):
    submitted = []
    monkeypatch.setattr(main._pool, "submit", lambda fn, *a: submitted.append(a))  # never run the pipeline
    monkeypatch.setattr(main, "_jobs", {})
    c = TestClient(main.app)
    c.submitted = submitted
    return c


def test_status_reports_styles(client):
    data = client.get("/api/status").json()
    assert set(data["styles"]) == {"yellow", "green", "beast", "anton", "bebas", "clean", "reveal"}
    look = data["style_looks"]["yellow"]
    assert look["font"] == "Arial Black" and look["upper"] is True and look["active"].startswith("#")
    assert "missing" in data


def test_create_job_queues_it(client):
    res = client.post("/api/jobs", json={"url": YT, "clip_count": 3})
    assert res.status_code == 200
    job = res.json()
    assert job["status"] == "queued" and job["options"]["clip_count"] == 3
    assert len(client.submitted) == 1
    assert client.get(f"/api/jobs/{job['id']}").json()["id"] == job["id"]


def test_free_plan_forces_free_features_on_the_server(client, monkeypatch):
    free_user = {
        "_id": "test-user", "email": "free@example.com", "role": "user", "plan": "free",
        "used": 0, "cycle": "",
    }
    monkeypatch.setitem(client.app.dependency_overrides, accounts.current_user, lambda: free_user)
    response = client.post("/api/jobs", json={
        "url": YT, "clip_count": 8, "min_len": 15, "max_len": 30, "style": "green",
        "layout": "fit", "hook": False, "prompt": "premium feature", "start_time": 10,
        "end_time": 30, "aspect": "16:9", "trim_pauses": False,
    })

    assert response.status_code == 200
    options = response.json()["options"]
    assert options == {
        "clip_count": 1, "min_len": 15, "max_len": 30, "style": "yellow",
        "layout": "auto", "hook": True, "prompt": "", "start_time": None,
        "end_time": None, "aspect": "9:16", "trim_pauses": True,
    }


def test_free_plan_rejects_clip_lengths_above_30_seconds(client, monkeypatch):
    free_user = {
        "_id": "test-user", "email": "free@example.com", "role": "user", "plan": "free",
        "used": 0, "cycle": "",
    }
    monkeypatch.setitem(client.app.dependency_overrides, accounts.current_user, lambda: free_user)

    response = client.post("/api/jobs", json={"url": YT, "min_len": 15, "max_len": 31})

    assert response.status_code == 403
    assert "15 to 30 seconds" in response.json()["detail"]
    assert client.submitted == []


@pytest.mark.parametrize("body, message", [
    ({"url": "https://example.com/video"}, "YouTube"),
    ({"url": YT, "min_len": 60, "max_len": 30}, "shorter"),
    ({"url": YT, "style": "rainbow"}, "caption style"),
    ({"url": YT, "layout": "zoom"}, "layout"),
])
def test_create_job_rejects_bad_input(client, body, message):
    res = client.post("/api/jobs", json=body)
    assert res.status_code == 400
    assert message in res.json()["detail"]
    assert client.submitted == []


def test_clip_count_is_bounded(client):
    assert client.post("/api/jobs", json={"url": YT, "clip_count": 99}).status_code == 422


def test_short_links_are_accepted(client):
    assert client.post("/api/jobs", json={"url": "https://youtu.be/abc123"}).status_code == 200


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


def test_cannot_delete_running_job(client):
    job = client.post("/api/jobs", json={"url": YT}).json()
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 409


def test_delete_finished_job(client):
    job = client.post("/api/jobs", json={"url": YT}).json()
    main._jobs[job["id"]]["status"] = "done"
    assert client.delete(f"/api/jobs/{job['id']}").json() == {"ok": True}
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404


def _finished_job_with_clips(client, n=3):
    job = client.post("/api/jobs", json={"url": YT}).json()
    d = main._job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    clips = []
    for i in range(n):
        name = f"clip_{i + 1:02d}"
        (d / f"{name}.mp4").write_bytes(b"v")
        (d / f"{name}.jpg").write_bytes(b"j")
        clips.append({"title": name, "video": f"/media/{job['id']}/{name}.mp4", "thumb": f"/media/{job['id']}/{name}.jpg"})
    main._jobs[job["id"]].update(status="done", clips=clips)
    return job["id"], d


def test_delete_one_clip_removes_it_and_its_files(client):
    job_id, d = _finished_job_with_clips(client)
    assert client.delete(f"/api/jobs/{job_id}/clips/1").json() == {"ok": True}
    titles = [c["title"] for c in client.get(f"/api/jobs/{job_id}").json()["clips"]]
    assert titles == ["clip_01", "clip_03"]
    assert not (d / "clip_02.mp4").exists() and not (d / "clip_02.jpg").exists()
    assert (d / "clip_01.mp4").exists()


def test_clip_download_uses_an_owned_attachment_response(client):
    job_id, _ = _finished_job_with_clips(client, 1)

    response = client.get(f"/api/jobs/{job_id}/clips/0/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.content == b"v"


def test_delete_clip_out_of_range_is_404(client):
    job_id, _ = _finished_job_with_clips(client, 1)
    assert client.delete(f"/api/jobs/{job_id}/clips/5").status_code == 404


def test_cannot_delete_clip_while_job_runs(client):
    job_id, _ = _finished_job_with_clips(client)
    main._jobs[job_id]["status"] = "running"
    assert client.delete(f"/api/jobs/{job_id}/clips/0").status_code == 409
