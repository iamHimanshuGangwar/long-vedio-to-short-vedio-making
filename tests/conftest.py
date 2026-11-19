"""Shared test setup.

Tests never touch the real data folder, the network, ffmpeg or an AI model:
everything that would is either a pure function tested directly or patched.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before app.config is imported anywhere.
os.environ["CLIPSMITH_DATA_DIR"] = tempfile.mkdtemp(prefix="clipsmith-test-")
# FastAPI's TestClient sends "Host: testserver"; real use is 127.0.0.1/localhost only.
os.environ["CLIPSMITH_ALLOWED_HOSTS"] = "testserver"

from app.source import Word  # noqa: E402


def make_words(text: str, start: float = 0.0, step: float = 0.4) -> list[Word]:
    """Evenly spaced words; a "|" in the text inserts a 1.5 s pause."""
    words, t = [], start
    for tok in text.split():
        if tok == "|":
            t += 1.5
            continue
        words.append(Word(tok, t, t + step * 0.9))
        t += step
    return words


@pytest.fixture
def words_factory():
    return make_words


@pytest.fixture
def no_ai(monkeypatch):
    """Force rule-based selection regardless of the developer's .env."""
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AI_PROVIDER", "none")
