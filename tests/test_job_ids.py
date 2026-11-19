"""Job IDs end up in file paths, so anything that isn't a real job ID is refused
before it gets near the filesystem (CodeQL py/path-injection)."""
import pytest
from fastapi.testclient import TestClient

from app import config, main


@pytest.mark.parametrize("bad", ["..", "../x", "..\\x", "a/b", "C:\\Windows", "", "x" * 11, "ABCDEF1234", "zzzzzzzzzz"])
def test_job_dir_refuses_anything_but_a_job_id(bad):
    with pytest.raises(ValueError):
        main._job_dir(bad)


def test_job_dir_accepts_real_ids():
    import os
    # compared resolved: on Windows the temp folder may be a short (8.3) name
    assert os.path.realpath(main._job_dir("0123abcdef")) == os.path.realpath(config.JOBS_DIR / "0123abcdef")


@pytest.mark.parametrize("path", ["/api/jobs/..", "/api/jobs/%2E%2E", "/api/jobs/..%5Cdata"])
def test_api_rejects_path_like_ids(monkeypatch, path):
    monkeypatch.setattr(main, "_jobs", {})
    res = TestClient(main.app).delete(path)
    assert res.status_code in (404, 400)


def test_loading_ignores_folders_with_bad_ids(tmp_path, monkeypatch):
    bad = config.JOBS_DIR / "not-a-job"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "job.json").write_text('{"id": "../../evil", "status": "done", "clips": [], "created": 1}', encoding="utf-8")
    monkeypatch.setattr(main, "_jobs", {})
    main._load_existing()
    assert "../../evil" not in main._jobs


def test_job_dir_stays_inside_the_jobs_folder():
    """The path is resolved and checked to be inside JOBS_DIR (the pattern CodeQL
    recognises for py/path-injection), on top of the ID format check."""
    import os
    path = main._job_dir("0123abcdef")
    root = os.path.realpath(config.JOBS_DIR)
    assert os.path.realpath(path).startswith(root + os.sep)


def test_job_dir_refuses_paths_that_resolve_outside(monkeypatch):
    # even if the ID pattern were ever loosened, a path outside JOBS_DIR is refused
    monkeypatch.setattr(main, "_JOB_ID", __import__("re").compile(r".+"))
    with pytest.raises(ValueError):
        main._job_dir("../outside")
