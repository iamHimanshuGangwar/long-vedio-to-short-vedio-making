import pytest
from fastapi.testclient import TestClient

from app import main

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


def test_delete_clip_out_of_range_is_404(client):
    job_id, _ = _finished_job_with_clips(client, 1)
    assert client.delete(f"/api/jobs/{job_id}/clips/5").status_code == 404


def test_cannot_delete_clip_while_job_runs(client):
    job_id, _ = _finished_job_with_clips(client)
    main._jobs[job_id]["status"] = "running"
    assert client.delete(f"/api/jobs/{job_id}/clips/0").status_code == 409


def test_cannot_delete_clip_while_any_clip_uploads(client):
    # uploads track their clip by position, so a removal would shift them onto the wrong clip
    job_id, _ = _finished_job_with_clips(client)
    main._jobs[job_id]["clips"][2]["youtube"] = {"status": "uploading", "progress": 10}
    assert client.delete(f"/api/jobs/{job_id}/clips/0").status_code == 409
    assert len(main._jobs[job_id]["clips"]) == 3
