@echo off
cd /d "%~dp0"
where ffmpeg >nul 2>&1 || echo WARNING: ffmpeg not found. Install it: winget install Gyan.FFmpeg
if not exist .venv (
  echo Creating Python environment...
  python -m venv .venv || (echo Python not found. Install Python 3.10+ from python.org & pause & exit /b 1)
)
echo Installing dependencies (this can take a few minutes)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
if not exist .env copy .env.example .env >nul
echo.
echo Done. If you haven't yet, download the AI model:  ollama pull qwen3.5:4b
echo Then double-click run.bat
pause
