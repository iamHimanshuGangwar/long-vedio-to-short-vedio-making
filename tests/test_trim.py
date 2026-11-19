"""Removing pauses and filler words from a clip (issue #1).

Pauses are measured from the audio (ffmpeg silencedetect): caption timings
can't be trusted for gaps, since human-written captions are spread evenly
across each line. Fillers come from the transcript and are only cut when
their timing is clean."""
import shutil
import subprocess

import pytest

from app import render, trim
from app.source import Word

SILENCEDETECT = """\
[Parsed_silencedetect_0 @ 0x1] silence_start: 1.999955
[Parsed_silencedetect_0 @ 0x1] silence_end: 3.500045 | silence_duration: 1.500091
size=N/A time=00:00:05.50 bitrate=N/A speed= 200x
[Parsed_silencedetect_0 @ 0x1] silence_start: 7.2
"""


def test_parse_silencedetect_output():
    # a silence still running at the end of the clip closes at the clip end
    assert trim.parse_silences(SILENCEDETECT, clip_len=8.0) == [(1.999955, 3.500045), (7.2, 8.0)]


def test_parse_ignores_garbage():
    assert trim.parse_silences("nothing here\nsilence_start: oops", clip_len=5) == []


# ---------- fillers ----------

def W(text, s, e):
    return Word(text, s, e)


def test_standalone_fillers_are_cut():
    words = [W("house", 15.6, 16.5), W("uh", 16.6, 17.0), W("I", 17.0, 17.12), W("Um,", 20.0, 20.4), W("so", 20.5, 20.7)]
    assert trim.filler_spans(words, clip_start=10.0) == [(6.6, 7.0), (10.0, 10.4)]


def test_overlapping_or_tiny_fillers_are_left_alone():
    words = [
        W("thing.", 544.6, 545.5), W("Uh", 548.4, 548.48), W("that's", 548.4, 548.7),  # overlaps next word
        W("uh", 550.0, 550.03),  # too short to be a reliable timing
    ]
    assert trim.filler_spans(words, clip_start=540.0) == []


def test_like_and_normal_words_are_never_fillers():
    words = [W("like", 1.0, 1.3), W("ah", 2.0, 2.4), W("hmm", 3.0, 3.5), W("so", 4.0, 4.3)]
    spans = trim.filler_spans(words, clip_start=0)
    assert (1.0, 1.3) not in spans and (4.0, 4.3) not in spans


# ---------- keep plan ----------

def test_keep_segments_leave_a_little_breathing_room():
    keep = trim.keep_segments(clip_len=10.0, cuts=[(2.0, 4.0)], pad=0.15)
    assert keep == [(0.0, 2.15), (3.85, 10.0)]


def test_short_cuts_are_ignored_and_overlapping_cuts_merge():
    keep = trim.keep_segments(clip_len=10.0, cuts=[(1.0, 1.2), (3.0, 5.0), (4.5, 6.0)], pad=0.1, min_cut=0.3)
    assert keep == [(0.0, 3.1), (5.9, 10.0)]


def test_no_cuts_keeps_everything():
    assert trim.keep_segments(clip_len=7.5, cuts=[]) == [(0.0, 7.5)]


def test_cuts_at_the_edges():
    keep = trim.keep_segments(clip_len=10.0, cuts=[(0.0, 1.5), (9.0, 10.0)], pad=0.1)
    assert keep == [(1.4, 9.1)]


def test_remap_times_onto_the_shortened_clip():
    keep = [(0.0, 2.0), (4.0, 10.0)]
    assert trim.remap(1.0, keep) == 1.0
    assert trim.remap(5.0, keep) == 3.0
    assert trim.remap(3.0, keep) is None  # inside a cut


def test_remap_words_drops_cut_words_and_shifts_the_rest():
    keep = [(0.0, 2.0), (4.0, 10.0)]
    words = [W("a", 11.0, 11.5), W("uh", 12.5, 13.0), W("b", 15.0, 15.5)]
    out = trim.remap_words(words, clip_start=10.0, keep=keep)
    assert [(w.text, w.start, w.end) for w in out] == [("a", 1.0, 1.5), ("b", 3.0, 3.5)]


def test_select_expression():
    assert trim.select_expr([(0.0, 2.15), (3.85, 10.0)]) == "between(t,0.000,2.150)+between(t,3.850,10.000)"


# ---------- real render ----------

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")


def _duration(path, stream):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "stream=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    return float(out)


@needs_ffmpeg
def test_real_clip_loses_its_silence_and_stays_in_sync(tmp_path):
    # 2 s tone, 2 s silence, 2 s tone, with video throughout
    src = tmp_path / "talk.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=6:size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=2",
        "-filter_complex", "[2]atrim=duration=2[s];[1][s][3]concat=n=3:v=0:a=1[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-shortest", str(src),
    ], check=True)
    silences = trim.detect_silences(src, 0, 6)
    assert len(silences) == 1 and abs(silences[0][0] - 2.0) < 0.1 and abs(silences[0][1] - 4.0) < 0.1

    keep = trim.keep_segments(6.0, silences)
    render.render_clip(src, [], 0, 6, tmp_path, "clip", layout="fit", keep=keep)
    out = tmp_path / "clip.mp4"
    v, a = _duration(out, "v:0"), _duration(out, "a:0")
    expected = sum(e - s for s, e in keep)  # about 4.3 s instead of 6
    assert abs(v - expected) < 0.2 and abs(a - expected) < 0.2
    assert abs(v - a) < 0.1  # picture and sound stay in sync


# ---------- option and pipeline ----------

from fastapi.testclient import TestClient  # noqa: E402

from app import main, source  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main._pool, "submit", lambda *a: None)
    monkeypatch.setattr(main, "_jobs", {})
    return TestClient(main.app)


def test_trim_is_on_by_default_and_can_be_turned_off(client):
    on = client.post("/api/jobs", json={"url": "https://youtu.be/x"}).json()
    off = client.post("/api/jobs", json={"url": "https://youtu.be/x", "trim_pauses": False}).json()
    assert on["options"]["trim_pauses"] is True and off["options"]["trim_pauses"] is False


def _fake_pipeline(monkeypatch, tmp_path):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"x")
    words = [Word(f"w{i}.", i * 1.0, i * 1.0 + 0.5) for i in range(120)]
    src = source.Source(video, "T", 120.0, "en", words, "youtube-captions")
    monkeypatch.setattr(source, "download", lambda *a, **k: src)
    from app import selector
    clip = selector.Clip(10.0, 50.0, "t", "", 80, "r", "d", [])
    monkeypatch.setattr(selector, "select", lambda *a, **k: ([clip], "basic", None))
    seen = {}

    def fake_render(*a, **k):
        seen.update(k)
        return {"layout": "fit", "length": 30.5 if k.get("keep") else 40.0}

    monkeypatch.setattr(render, "render_clip", fake_render)
    monkeypatch.setattr(trim, "plan", lambda *a, **k: [(0.0, 10.0), (19.5, 40.0)])
    return seen


@pytest.mark.parametrize("enabled", [True, False])
def test_pipeline_passes_the_trim_plan_to_the_renderer(monkeypatch, tmp_path, enabled):
    seen = _fake_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_jobs", {})
    req = main.JobRequest(url="https://youtu.be/x", trim_pauses=enabled)
    job = main._new_job(req, "00000000aa")
    main._run(job["id"], req)
    done = main._jobs["00000000aa"]
    assert done["status"] == "done", done.get("error")
    assert (seen.get("keep") is not None) is enabled
    assert done["clips"][0]["length"] == (30.5 if enabled else 40.0)  # the real, trimmed length


def test_tiny_wordless_scraps_between_cuts_are_dropped(monkeypatch, tmp_path):
    """Two silences 0.1 s apart leave a ~0.3 s scrap with no speech in it,
    which would flash on screen between two jump cuts."""
    monkeypatch.setattr(trim, "detect_silences", lambda *a: [(16.0, 17.2), (17.3, 19.1)])
    words = [Word("hi", 1.0, 1.4), Word("there", 30.0, 30.4)]
    keep = trim.plan(tmp_path / "v.mp4", words, 0, 40)
    assert all(e - s >= 0.5 for s, e in keep)
    assert keep == [(0.0, 16.15), (18.95, 40.0)]


def test_short_pieces_with_speech_are_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(trim, "detect_silences", lambda *a: [(16.0, 17.2), (17.6, 19.1)])
    words = [Word("yes", 17.25, 17.5)]
    keep = trim.plan(tmp_path / "v.mp4", words, 0, 40)
    assert any(s <= 17.25 <= e for s, e in keep)


# ---------- review fixes (#23) ----------

def test_short_piece_is_kept_when_a_word_runs_into_it(monkeypatch, tmp_path):
    """The word starts inside the first pause (rough timing) but its sound is in the scrap."""
    # cuts become 16.15-17.35 and 17.75-18.95, leaving a 0.4 s piece 17.35-17.75;
    # the word starts at 17.2 (inside the first cut) but is spoken into the piece
    monkeypatch.setattr(trim, "detect_silences", lambda *a: [(16.0, 17.5), (17.6, 19.1)])
    words = [Word("yes", 17.2, 17.6)]
    keep = trim.plan(tmp_path / "v.mp4", words, 0, 40)
    assert any(s < 17.6 and e > 17.2 for s, e in keep)


def test_caption_kept_for_a_word_that_starts_before_the_clip():
    keep = [(0.0, 10.0)]
    words = [Word("hello", 9.9, 10.4), Word("there", 10.5, 10.9)]
    out = trim.remap_words(words, clip_start=10.0, keep=keep)
    assert [(w.text, w.start) for w in out] == [("hello", 0.0), ("there", 0.5)]


def test_caption_kept_for_a_word_that_starts_inside_a_cut():
    keep = [(0.0, 2.0), (4.0, 10.0)]
    words = [Word("late", 13.8, 14.6)]  # 3.8-4.6 in clip time: starts in the cut, ends after it
    out = trim.remap_words(words, clip_start=10.0, keep=keep)
    assert [(w.text, w.start, w.end) for w in out] == [("late", 2.0, 2.6)]


def test_word_entirely_inside_a_cut_is_dropped():
    keep = [(0.0, 2.0), (4.0, 10.0)]
    assert trim.remap_words([Word("uh", 12.5, 13.0)], clip_start=10.0, keep=keep) == []


@needs_ffmpeg
def test_trimming_keeps_every_audio_track(tmp_path):
    src = tmp_path / "two_tracks.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=4",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-shortest", str(src),
    ], check=True)
    keep = [(0.0, 1.5), (2.5, 4.0)]
    render.render_clip(src, [], 0, 4, tmp_path, "clip", layout="fit", keep=keep)
    out = tmp_path / "clip.mp4"
    tracks = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=duration",
                             "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.split()
    assert len(tracks) == 2
    assert all(abs(float(d) - 3.0) < 0.2 for d in tracks)
