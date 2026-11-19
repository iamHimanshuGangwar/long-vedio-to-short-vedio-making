"""Download a YouTube video and get a word-level transcript for it."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import yt_dlp

from . import config

Progress = Callable[[float, str], None]


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Source:
    video_path: Path
    title: str
    duration: float
    language: str
    words: list[Word]
    transcript_source: str  # "youtube-captions" or "whisper"


def _ydl_base() -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    # yt-dlp needs a JavaScript runtime for YouTube; it finds Deno itself,
    # Node has to be enabled explicitly.
    if not shutil.which("deno") and shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
    return opts


def download(
    url: str, workdir: Path, progress: Progress, max_duration_seconds: int | None = None
) -> Source:
    progress(2, "Reading video details")
    with yt_dlp.YoutubeDL(_ydl_base()) as ydl:
        info = ydl.extract_info(url, download=False)

    duration = float(info.get("duration") or 0)
    if duration and duration > config.MAX_VIDEO_HOURS * 3600:
        raise ValueError(f"Video is longer than {config.MAX_VIDEO_HOURS} hours.")
    if duration and max_duration_seconds is not None and duration > max_duration_seconds:
        raise ValueError(f"This plan supports source videos up to {max_duration_seconds // 60} minutes.")
    if info.get("is_live"):
        raise ValueError("Live streams can't be clipped until they finish.")

    lang = (info.get("language") or "en").split("-")[0]

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                progress(3 + 22 * d.get("downloaded_bytes", 0) / total, "Downloading video")

    opts = _ydl_base() | {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "retries": 5,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    video_path = workdir / "source.mp4"
    if not video_path.exists():
        found = [p for p in workdir.glob("source.*") if p.suffix in (".mkv", ".webm", ".mp4")]
        if not found:
            raise RuntimeError("Download finished but no video file was produced.")
        video_path = found[0]

    words: list[Word] = []
    transcript_source = "youtube-captions"
    track = pick_caption_track(info, lang)
    if track:
        progress(25, "Getting captions")
        words = _fetch_captions(url, workdir, track)
    if not words:
        progress(26, "No captions from YouTube, transcribing audio (slower)")
        words = whisper_transcribe(video_path, workdir, progress)
        transcript_source = "whisper"
    if not words:
        raise RuntimeError("Could not get any speech from this video.")

    return Source(
        video_path=video_path,
        title=info.get("title") or "Untitled",
        duration=duration or words[-1].end,
        language=lang,
        words=words,
        transcript_source=transcript_source,
    )


def pick_caption_track(info: dict, lang: str) -> str | None:
    """The one caption track worth fetching: manual captions beat automatic ones,
    and the video's own language beats English. Asking YouTube for a single track
    (not every variant) keeps us clear of its rate limit."""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for pool, codes in ((manual, [lang, "en"]), (auto, [f"{lang}-orig", lang, "en-orig", "en"])):
        for code in codes:
            if code in pool:
                return code
    return None


def _fetch_captions(url: str, workdir: Path, track: str) -> list[Word]:
    """Captions are a shortcut, not a requirement: if YouTube refuses them
    (e.g. HTTP 429 Too Many Requests), return nothing and let Whisper transcribe."""
    opts = _ydl_base() | {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [track],
        "subtitlesformat": "json3",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError:
        return []
    sub = workdir / f"source.{track}.json3"
    return parse_json3(sub) if sub.exists() else []


def from_local_file(
    path: Path,
    workdir: Path,
    progress: Progress,
    title: str | None = None,
    max_duration_seconds: int | None = None,
) -> Source:
    """An uploaded video: no captions to fetch, so transcribe it with Whisper."""
    progress(2, "Reading video details")
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height",
         "-of", "json", str(path)], capture_output=True, text=True,
    )
    if out.returncode:
        detail = out.stderr.strip()
        raise ValueError(f"Could not inspect the uploaded video: {detail or 'ffprobe failed.'}")
    try:
        details = json.loads(out.stdout)
        duration = float(details["format"]["duration"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise ValueError("That file doesn't look like a video ffmpeg can read.") from None
    if os.environ.get("APP_ENV") == "production":
        video = next((stream for stream in details.get("streams", []) if "width" in stream and "height" in stream), None)
        if not video:
            raise ValueError("No video track was found in the uploaded file.")
        if int(video["width"]) * int(video["height"]) > 1920 * 1080:
            raise ValueError("For stable processing on the free server, upload video at 1080p or lower.")
    if duration > config.MAX_VIDEO_HOURS * 3600:
        raise ValueError(f"Video is longer than {config.MAX_VIDEO_HOURS} hours.")
    if max_duration_seconds is not None and duration > max_duration_seconds:
        raise ValueError(f"This plan supports source videos up to {max_duration_seconds // 60} minutes.")

    progress(26, "Transcribing audio")
    words = whisper_transcribe(path, workdir, progress)
    if not words:
        raise RuntimeError("Could not get any speech from this video.")
    return Source(
        video_path=path,
        title=title or path.stem or "Untitled",
        duration=duration or words[-1].end,
        language="",
        words=words,
        transcript_source="whisper",
    )


# A token of only dashes is a speaker-change mark in manual captions ("- Joe's dish").
_NOISE = re.compile(r"^\[.*\]$|^\(.*\)$|^>>+$|^♪+$|^[-–—]+$")


def _clean(token: str) -> str:
    token = token.replace("\n", " ").strip()
    token = token.replace(">>", "").strip()
    return "" if _NOISE.match(token) else token


def parse_json3(path: Path) -> list[Word]:
    """YouTube json3 captions -> words. Auto captions carry per-word offsets;
    manual captions carry whole lines, which are spread evenly across the line."""
    data = json.loads(path.read_text(encoding="utf-8"))
    words: list[Word] = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        ev_start = ev.get("tStartMs", 0) / 1000
        ev_end = ev_start + ev.get("dDurationMs", 0) / 1000
        timed = any("tOffsetMs" in s for s in segs[1:])
        if not timed:
            # Manual captions: the whole line, possibly split over several segs
            # with no offsets. Joined and spread evenly below; giving every word
            # the line's start time made them zero-length, so they were skipped.
            # Each seg is cleaned first so a multi-word tag like "[Music playing]"
            # is still dropped whole rather than split into caption words.
            segs = [{"utf8": " ".join(_clean(s.get("utf8", "")) for s in segs)}]
        if timed:
            for s in segs:
                tok = _clean(s.get("utf8", ""))
                if not tok:
                    continue
                start = ev_start + s.get("tOffsetMs", 0) / 1000
                for part in tok.split():
                    words.append(Word(part, start, ev_end))
        else:
            parts = [p for p in (_clean(t) for t in segs[0].get("utf8", "").split()) if p]
            if not parts:
                continue
            step = max(ev_end - ev_start, 0.2) / len(parts)
            for i, part in enumerate(parts):
                words.append(Word(part, ev_start + i * step, ev_start + (i + 1) * step))

    words.sort(key=lambda w: w.start)
    # A word ends when the next one starts (captions overlap), capped so
    # a word before a pause doesn't linger on screen.
    for i, w in enumerate(words):
        nxt = words[i + 1].start if i + 1 < len(words) else w.start + 0.6
        w.end = max(w.start + 0.08, min(nxt, w.start + 0.9))
    return words


def read_wav_16k(path: Path) -> np.ndarray:
    """Mono 16-bit 16 kHz WAV (as ffmpeg writes it above) -> float32 in [-1, 1)."""
    with wave.open(str(path), "rb") as w:
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def _has_audio_track(video_path: Path) -> bool:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    if probe.returncode:
        detail = probe.stderr.strip()
        raise RuntimeError(f"Could not inspect the video's audio track: {detail or 'ffprobe failed.'}")
    return bool(probe.stdout.strip())


def whisper_transcribe(video_path: Path, workdir: Path, progress: Progress) -> list[Word]:
    if not _has_audio_track(video_path):
        raise ValueError(
            "This video has no audio track. ReelCraft AI needs audio to transcribe speech and find clip moments. "
            "Please upload a video with audible speech."
        )

    if config.GROQ_API_KEY:
        return _groq_transcribe(video_path, workdir, progress)
    if os.environ.get("APP_ENV") == "production":
        raise RuntimeError("Audio transcription is not configured. Add GROQ_API_KEY in the deployment environment.")

    from faster_whisper import WhisperModel

    wav = workdir / "audio.wav"
    extracted = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-threads", "1", "-i", str(video_path),
         "-vn", "-ac", "1", "-ar", "16000", str(wav)],
        capture_output=True, text=True,
    )
    if extracted.returncode:
        wav.unlink(missing_ok=True)
        detail = extracted.stderr.strip()
        raise RuntimeError(f"Could not extract audio from the video: {detail or 'ffmpeg failed.'}")
    model = WhisperModel(
        config.WHISPER_MODEL, device="cpu", compute_type="int8", download_root=str(config.MODELS_DIR / "whisper")
    )
    # Pass samples, not the path: faster-whisper's own decoder breaks on PyAV >= 15.
    segments, info = model.transcribe(read_wav_16k(wav), word_timestamps=True, vad_filter=True)
    total = info.duration or 1
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            tok = w.word.strip()
            if tok:
                words.append(Word(tok, float(w.start), float(w.end)))
        progress(26 + 14 * min(seg.end / total, 1), "Transcribing audio")
    wav.unlink(missing_ok=True)
    return words


def _groq_transcribe(video_path: Path, workdir: Path, progress: Progress) -> list[Word]:
    from groq import Groq

    audio = workdir / "audio.mp3"
    extracted = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-threads", "1", "-i", str(video_path),
         "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio)],
        capture_output=True, text=True,
    )
    if extracted.returncode:
        audio.unlink(missing_ok=True)
        detail = extracted.stderr.strip()
        raise RuntimeError(f"Could not extract audio from the video: {detail or 'ffmpeg failed.'}")
    try:
        if audio.stat().st_size > 24 * 1024 * 1024:
            raise ValueError("The audio is too large for the free transcription service. Use a shorter video.")
        progress(30, "Transcribing audio with Groq")
        with audio.open("rb") as stream:
            result = Groq(api_key=config.GROQ_API_KEY).audio.transcriptions.create(
                file=(audio.name, stream),
                model=os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"),
                response_format="verbose_json",
                timestamp_granularities=["word"],
            )
        words = [
            Word(str(item.word).strip(), float(item.start), float(item.end))
            for item in (getattr(result, "words", None) or [])
            if str(getattr(item, "word", "")).strip()
        ]
        if not words:
            raise RuntimeError("The transcription service returned no word timestamps.")
        return words
    finally:
        audio.unlink(missing_ok=True)
