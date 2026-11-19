import json

from app import main


def test_interrupted_job_is_failed_and_unused_quota_marked_refunded(tmp_path, monkeypatch):
    monkeypatch.setattr(main.config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(main.config, "MONGODB_URI", "")
    job = {
        "id": "0123456789", "status": "running", "owner_id": "test-user",
        "plan": "normal", "quota_period": "2026-11", "quota_reserved": 3,
        "clips": [{"video": "kept.mp4"}], "quota_refunded": False,
    }

    main._recover_interrupted_job(job)

    assert job["status"] == "error"
    assert "server restarted" in job["error"]
    assert job["quota_refunded"] is True
    persisted = json.loads((tmp_path / "jobs" / job["id"] / "job.json").read_text())
    assert persisted["status"] == "error"
