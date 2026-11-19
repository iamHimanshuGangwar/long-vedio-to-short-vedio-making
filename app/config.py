"""Paths and settings shared across the app."""
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# override: the app's .env wins over machine-wide variables (e.g. an older OLLAMA_MODELS)
load_dotenv(ROOT / ".env", override=True)

DATA_DIR = Path(os.environ.get("CLIPSMITH_DATA_DIR") or ("/tmp/reelcraft" if os.environ.get("APP_ENV") == "production" else ROOT / "data"))
JOBS_DIR = DATA_DIR / "jobs"
MODELS_DIR = DATA_DIR / "models"
STATIC_DIR = ROOT / "static"

for _d in (JOBS_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Keep model downloads inside the project instead of the user profile.
os.environ.setdefault("HF_HOME", str(MODELS_DIR / "hf"))

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "tiny")
MAX_SOURCE_MINUTES = max(1, min(10, int(os.environ.get("MAX_SOURCE_MINUTES", "5"))))

OUT_W, OUT_H = 1080, 1920
MAX_VIDEO_HOURS = 4


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:4b")
# Explicit path from .env, else whatever "ollama" is on PATH (a normal install).
OLLAMA_EXE = os.environ.get("OLLAMA_EXE", "") or shutil.which("ollama") or ""
OLLAMA_MODELS_DIR = os.environ.get("OLLAMA_MODELS", "")
OLLAMA_CTX = int(os.environ.get("OLLAMA_CTX", "8192"))

MONGODB_URI = os.environ.get("MONGODB_URI", "")
MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE", "reelcraft")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
BLOCKED_EMAIL_DOMAINS = os.environ.get("BLOCKED_EMAIL_DOMAINS", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL", "")
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")


def ai_provider() -> str | None:
    """Which AI picks the moments: "ollama" (local), "gemini", "claude", or None.
    AI_PROVIDER in .env forces one; otherwise whichever is configured wins."""
    forced = os.environ.get("AI_PROVIDER", "").strip().lower()
    if forced in ("ollama", "gemini", "claude"):
        return forced
    if forced in ("none", "basic"):
        return None
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "claude"
    if OLLAMA_EXE and os.path.exists(OLLAMA_EXE):
        return "ollama"
    return None


def missing_tools() -> list[str]:
    """Programs the pipeline shells out to."""
    return [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
