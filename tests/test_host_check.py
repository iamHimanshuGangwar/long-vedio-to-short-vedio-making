"""DNS rebinding: a page on evil.example can point its own name at 127.0.0.1,
so its requests reach this app with "Host: evil.example:8765" and a matching
Origin. Only requests addressed to this machine's own names are answered."""
import pytest
from fastapi.testclient import TestClient

from app import main, youtube


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(youtube, "disconnect", lambda: seen.append("disconnect"))
    monkeypatch.setattr(main, "_jobs", {})
    return seen


@pytest.mark.parametrize("host", ["evil.example:8765", "evil.example", "127.0.0.1.evil.example:8765", "192.168.1.5:8765"])
def test_rebound_hostnames_are_refused(calls, host):
    client = TestClient(main.app, base_url=f"http://{host}")
    origin = f"http://{host}"
    assert client.post("/api/youtube/disconnect", headers={"Origin": origin}).status_code == 403
    assert client.get("/api/jobs").status_code == 403  # can't read data either
    assert client.get("/api/youtube").status_code == 403
    assert calls == []


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "127.0.0.1", "[::1]:8765"])
def test_local_addresses_work(calls, host):
    client = TestClient(main.app, base_url=f"http://{host}")
    assert client.get("/api/jobs").status_code == 200
    assert client.post("/api/youtube/disconnect", headers={"Origin": f"http://{host}"}).status_code == 200
    assert calls == ["disconnect"]
