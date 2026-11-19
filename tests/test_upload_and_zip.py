import io
import shutil
import subprocess
import zipfile
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import main, source
from app.source import Word


@pytest.fixture
def client(monkeypatch):
    submitted = []
    monkeypatch.setattr(main._pool, "submit", lambda fn, *a: submitted.append(a))  # never run the pipeline
    monkeypatch.setattr(main, "_jobs", {})
    c = TestClient(main.app)
    c.submitted = submitted
    return c


def _upload(client, name="My Talk.mp4", data=b"fake video bytes", **fields):
    return client.post("/api/jobs/upload", files={"file": (name, io.BytesIO(data), "video/mp4")}, data=fields)


def test_upload_is_queued_and_saved(client):
    res = _upload(client, clip_count="3", style="yellow")
    assert res.status_code == 200
    job = res.json()
    assert job["status"] == "queued" and job["upload"] == "My Talk.mp4"
    assert job["options"]["clip_count"] == 3
    assert len(client.submitted) == 1
    saved = main._job_dir(job["id"]) / "source.mp4"
    assert saved.read_bytes() == b"fake video bytes"


def test_upload_carries_prompt_and_time_range(client):
    job = _upload(client, prompt="the money part", start_time="60", end_time="600").json()
    assert job["options"]["prompt"] == "the money part"
    assert job["options"]["start_time"] == 60 and job["options"]["end_time"] == 600


def test_upload_carries_aspect(client):
    assert _upload(client, aspect="1:1").json()["options"]["aspect"] == "1:1"
    assert _upload(client, aspect="4:5").status_code == 400


def test_upload_rejects_backwards_time_range(client):
    res = _upload(client, start_time="600", end_time="60")
    assert res.status_code == 400 and "'To' time" in res.json()["detail"]
    assert client.submitted == []


@pytest.mark.parametrize("name", ["notes.txt", "video", "clip.avi"])
def test_upload_rejects_bad_extension(client, name):
    res = _upload(client, name=name)
    assert res.status_code == 400
    assert "mp4" in res.json()["detail"]
    assert client.submitted == []


def test_upload_rejects_bad_options(client):
    res = _upload(client, min_len="60", max_len="30")
    assert res.status_code == 400
    assert client.submitted == []


def _finished_job(client, clips=2):
    job = _upload(client).json()
    d = main._job_dir(job["id"])
    entries = []
    for i in range(clips):
        name = f"clip_{i + 1:02d}"
        (d / f"{name}.mp4").write_bytes(f"video {i}".encode())
        entries.append({"video": f"/media/{job['id']}/{name}.mp4", "title": f"Clip {i}"})
    main._jobs[job["id"]].update(status="done", clips=entries, title="Great Talk: Part 1")
    return job["id"]


def test_zip_contains_all_clips(client):
    job_id = _finished_job(client)
    res = client.get(f"/api/jobs/{job_id}/zip")
    assert res.status_code == 200
    assert "Great Talk Part 1.zip" in unquote(res.headers["content-disposition"])
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        assert sorted(z.namelist()) == ["clip_01.mp4", "clip_02.mp4"]
        assert z.read("clip_02.mp4") == b"video 1"
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())


def test_zip_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope/zip").status_code == 404


def test_zip_without_clips_is_409(client):
    job_id = _finished_job(client, clips=0)
    assert client.get(f"/api/jobs/{job_id}/zip").status_code == 409


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg")
def test_from_local_file(tmp_path, monkeypatch):
    video = tmp_path / "Holiday Trip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240",
         "-f", "lavfi", "-i", "sine=duration=2", "-shortest", str(video)],
        check=True,
    )
    monkeypatch.setattr(source, "whisper_transcribe", lambda p, w, prog: [Word("hello", 0.1, 0.5)])
    src = source.from_local_file(video, tmp_path, lambda *a: None)
    assert src.title == "Holiday Trip"
    assert 1.5 < src.duration < 2.6
    assert src.transcript_source == "whisper"
    assert src.words[0].text == "hello"
    assert src.video_path == video


def test_upload_over_size_limit_is_rejected_and_removed(client, monkeypatch):
    monkeypatch.setattr(main, "_MAX_UPLOAD_BYTES", 10)
    main.config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    before = set(main.config.JOBS_DIR.iterdir())
    res = _upload(client, data=b"x" * 11)
    assert res.status_code == 413
    assert client.submitted == []
    assert set(main.config.JOBS_DIR.iterdir()) == before


def test_zip_is_not_left_on_disk(client):
    job_id = _finished_job(client)
    assert client.get(f"/api/jobs/{job_id}/zip").status_code == 200
    assert not list(main._job_dir(job_id).glob("*.zip*"))


def test_zip_skips_a_clip_deleted_while_zipping(client, monkeypatch):
    # a clip can be deleted from another tab while "Download all" is still building the zip
    job_id = _finished_job(client)
    real_write = zipfile.ZipFile.write

    def write_then_delete_next(self, filename, *a, **kw):
        real_write(self, filename, *a, **kw)
        (main._job_dir(job_id) / "clip_02.mp4").unlink(missing_ok=True)

    monkeypatch.setattr(zipfile.ZipFile, "write", write_then_delete_next)
    res = client.get(f"/api/jobs/{job_id}/zip")
    assert res.status_code == 200
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        assert z.namelist() == ["clip_01.mp4"]
