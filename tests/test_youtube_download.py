import json

import pytest
import yt_dlp

from app import main, source
from app.source import Word

INFO = {
    "title": "Big Minecraft video",
    "duration": 600,
    "language": "en",
    "subtitles": {},
    "automatic_captions": {"en-orig": [{}], "en": [{}], "de": [{}]},
}
CAPTIONS = {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hello world"}]}]}


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; records every set of options it was given."""

    calls: list[dict] = []
    fail_subtitles: Exception | None = None

    def __init__(self, opts):
        self.opts = opts
        FakeYDL.calls.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return dict(INFO)

    def download(self, urls):
        out = self.opts["outtmpl"].replace("%(ext)s", "")
        if self.opts.get("writesubtitles") or self.opts.get("writeautomaticsub"):
            if FakeYDL.fail_subtitles:
                raise FakeYDL.fail_subtitles
            for lang in self.opts["subtitleslangs"]:
                with open(f"{out}{lang}.json3", "w", encoding="utf-8") as f:
                    json.dump(CAPTIONS, f)
        if not self.opts.get("skip_download"):
            with open(f"{out}mp4", "wb") as f:
                f.write(b"video")


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.calls = []
    FakeYDL.fail_subtitles = None
    monkeypatch.setattr(source.yt_dlp, "YoutubeDL", FakeYDL)
    whisper_calls = []

    def fake_whisper(path, workdir, progress):
        whisper_calls.append(path)
        return [Word("from", 0.0, 0.4), Word("whisper", 0.4, 0.8)]

    monkeypatch.setattr(source, "whisper_transcribe", fake_whisper)
    FakeYDL.whisper_calls = whisper_calls
    return FakeYDL


def test_uses_youtube_captions_when_available(fake_ydl, tmp_path):
    src = source.download("https://youtu.be/x", tmp_path, lambda *a: None)
    assert src.transcript_source == "youtube-captions"
    assert [w.text for w in src.words] == ["hello", "world"]
    assert fake_ydl.whisper_calls == []


def test_caption_rate_limit_falls_back_to_whisper(fake_ydl, tmp_path):
    fake_ydl.fail_subtitles = yt_dlp.utils.DownloadError(
        "Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests"
    )
    src = source.download("https://youtu.be/x", tmp_path, lambda *a: None)
    assert src.transcript_source == "whisper"
    assert [w.text for w in src.words] == ["from", "whisper"]
    assert (tmp_path / "source.mp4").exists()


def test_only_one_caption_track_is_requested(fake_ydl, tmp_path):
    source.download("https://youtu.be/x", tmp_path, lambda *a: None)
    sub_requests = [o for o in fake_ydl.calls if o.get("writesubtitles") or o.get("writeautomaticsub")]
    assert len(sub_requests) == 1
    assert sub_requests[0]["subtitleslangs"] == ["en-orig"]


def test_no_captions_offered_means_no_caption_request(fake_ydl, tmp_path, monkeypatch):
    monkeypatch.setitem(INFO, "automatic_captions", {})
    src = source.download("https://youtu.be/x", tmp_path, lambda *a: None)
    assert src.transcript_source == "whisper"
    assert not [o for o in fake_ydl.calls if o.get("writesubtitles") or o.get("writeautomaticsub")]


@pytest.mark.parametrize("raw, expected", [
    ("\x1b[0;31mERROR:\x1b[0m [youtube] abc: Unable to download API page: Failed to resolve 'www.youtube.com' "
     "([Errno 11001] getaddrinfo failed)", "Couldn't reach YouTube"),
    ("\x1b[0;31mERROR:\x1b[0m Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests",
     "YouTube is limiting requests"),
    ("ERROR: [youtube] abc: Video unavailable. This video is private", "Video unavailable. This video is private"),
])
def test_friendly_error(raw, expected):
    msg = main.friendly_error(RuntimeError(raw))
    assert expected in msg
    assert "\x1b" not in msg and "[0;31m" not in msg and not msg.startswith("ERROR")
