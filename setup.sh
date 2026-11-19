#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null 2>&1 || echo "WARNING: ffmpeg not found. Install it (brew install ffmpeg / sudo apt install ffmpeg)."
if [ ! -d .venv ]; then
  echo "Creating Python environment..."
  python3 -m venv .venv
fi
echo "Installing dependencies (this can take a few minutes)..."
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
echo
echo "Done. If you haven't yet, download the AI model:  ollama pull qwen3.5:4b"
echo "Then run: ./run.sh"
