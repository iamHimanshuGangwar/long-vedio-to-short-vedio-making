"""Talk to a local model through Ollama. Free and offline, but CPU-bound,
so requests are slow and the transcript has to be sent in pieces."""
from __future__ import annotations

import os
import subprocess
import time

import requests

from . import config

_READ_TIMEOUT = 1800  # a single chunk can take many minutes on a laptop CPU


def _reachable() -> bool:
    try:
        return requests.get(f"{config.OLLAMA_URL}/api/version", timeout=3).status_code == 200
    except requests.RequestException:
        return False


def ensure_running() -> None:
    """Start the Ollama server if it isn't already up."""
    if _reachable():
        return
    exe = config.OLLAMA_EXE
    if not exe or not os.path.exists(exe):
        raise RuntimeError("Ollama isn't running and OLLAMA_EXE doesn't point to ollama.exe.")
    env = os.environ.copy()
    if config.OLLAMA_MODELS_DIR:
        env["OLLAMA_MODELS"] = config.OLLAMA_MODELS_DIR
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([exe, "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    for _ in range(40):
        time.sleep(0.5)
        if _reachable():
            return
    raise RuntimeError("Ollama was started but didn't respond.")


def chat_json(system: str, prompt: str, schema: dict) -> str:
    """One request, answer constrained to the JSON schema. Returns the raw JSON text."""
    ensure_running()
    res = requests.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={
            "model": config.OLLAMA_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "stream": False,
            "think": False,  # reasoning tokens are too slow on CPU for what they add here
            "format": schema,
            "keep_alive": "10m",
            "options": {"num_ctx": config.OLLAMA_CTX, "temperature": 0.3},
        },
        timeout=(5, _READ_TIMEOUT),
    )
    if res.status_code == 404:
        raise RuntimeError(
            f"Ollama is running but can't see {config.OLLAMA_MODEL}. It was probably started outside this app "
            f"with a different models folder; close Ollama (Stop-Process -Name ollama) and try again."
        )
    res.raise_for_status()
    return res.json()["message"]["content"]


def unload() -> None:
    """Free the model's memory; video encoding needs it next."""
    try:
        requests.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={"model": config.OLLAMA_MODEL, "keep_alive": 0},
            timeout=20,
        )
    except requests.RequestException:
        pass
