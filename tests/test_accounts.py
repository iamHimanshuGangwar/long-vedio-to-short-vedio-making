import pytest
from fastapi import HTTPException

from app import accounts


def test_signed_session_round_trip_and_tamper_detection(monkeypatch):
    monkeypatch.setattr(accounts.config, "SESSION_SECRET", "test-session-secret-" * 3)
    token = accounts.make_session("user-123")

    assert accounts.read_session(token) == "user-123"
    assert accounts.read_session(token + "x") is None


def test_session_signing_requires_a_strong_secret(monkeypatch):
    monkeypatch.setattr(accounts.config, "SESSION_SECRET", "too-short")

    with pytest.raises(HTTPException) as exc:
        accounts.make_session("user-123")

    assert exc.value.status_code == 503


def test_free_plan_has_the_configured_quota_and_locked_defaults():
    assert accounts.PLANS["free"]["quota"] == 1
    assert accounts.PLANS["free"]["period"] == "month"
    assert accounts.FREE_SETTINGS == {
        "clip_count": 1, "min_len": 15, "max_len": 30, "aspect": "9:16", "layout": "auto",
    }


def test_free_quota_is_one_clip_per_month(monkeypatch):
    month = accounts.datetime.now(accounts.timezone.utc).strftime("%Y-%m")

    class Users:
        def __init__(self):
            self.user = {"_id": "mongo-user", "email": "free@example.com", "plan": "free", "used": 0, "cycle": ""}

        def find_one_and_update(self, query, update, return_document):
            assert query["$or"] == [{"cycle": {"$ne": month}}, {"used": {"$lte": 0}}]
            assert update[0]["$set"]["cycle"] == month
            assert update[0]["$set"]["used"]["$cond"][0] == {"$eq": ["$cycle", month]}
            within_quota = self.user["cycle"] != month or self.user["used"] <= 0
            if not within_quota:
                return None
            self.user["used"] = self.user["used"] + 1 if self.user["cycle"] == month else 1
            self.user["cycle"] = month
            return self.user.copy()

    users = Users()
    monkeypatch.setattr(accounts, "mongo_db", lambda: type("DB", (), {"users": users})())
    user = {"_id": "mongo-user", "plan": "free"}

    assert accounts.reserve_quota(user, 1)["used"] == 1
    with pytest.raises(HTTPException) as exc:
        accounts.reserve_quota(user, 1)

    assert exc.value.status_code == 429
