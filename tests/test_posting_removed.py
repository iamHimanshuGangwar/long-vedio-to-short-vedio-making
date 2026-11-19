from fastapi.testclient import TestClient

from app.main import app


def test_youtube_publishing_routes_are_not_exposed():
    client = TestClient(app)
    for path, method in (
        ("/api/youtube", "get"),
        ("/api/youtube/connect", "get"),
        ("/api/youtube/disconnect", "post"),
        ("/api/jobs/0000000001/clips/0/youtube", "post"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 404
