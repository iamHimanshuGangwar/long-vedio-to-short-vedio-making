"""DNS rebinding protection and removal of shared YouTube publishing routes."""
import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.mark.parametrize("host", ["evil.example:8765", "evil.example", "127.0.0.1.evil.example:8765", "192.168.1.5:8765"])
def test_rebound_hostnames_are_refused(host):
    client = TestClient(main.app, base_url=f"http://{host}")
    origin = f"http://{host}"
    assert client.post("/api/auth/logout", headers={"Origin": origin}).status_code == 403
    assert client.get("/api/jobs").status_code == 403  # can't read data either
    assert client.get("/api/youtube").status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "127.0.0.1", "[::1]:8765"])
def test_local_addresses_work(host):
    client = TestClient(main.app, base_url=f"http://{host}")
    assert client.get("/api/jobs").status_code == 200
    assert client.post("/api/youtube/disconnect", headers={"Origin": f"http://{host}"}).status_code == 404
    assert client.get("/api/youtube").status_code == 404
