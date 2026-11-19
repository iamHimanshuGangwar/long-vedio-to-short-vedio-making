#!/usr/bin/env sh
cd "$(dirname "$0")"
[ -d .venv ] || { echo "Run ./setup.sh first."; exit 1; }
echo "Starting ReelCraft AI at http://127.0.0.1:8765  (Ctrl+C to stop)"
( sleep 2; (command -v open >/dev/null && open http://127.0.0.1:8765) || (command -v xdg-open >/dev/null && xdg-open http://127.0.0.1:8765) ) >/dev/null 2>&1 &
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
