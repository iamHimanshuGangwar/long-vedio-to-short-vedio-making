import json
import wave

import numpy as np
import pytest

from app import source
from app.source import parse_json3, read_wav_16k


def _write(tmp_path, events):
    p = tmp_path / "subs.json3"
    p.write_text(json.dumps({"events": events}), encoding="utf-8")
    return p


def test_auto_captions_use_per_word_offsets(tmp_path):
    p = _write(tmp_path, [
        {"tStartMs": 1000, "dDurationMs": 2000,
         "segs": [{"utf8": "hello"}, {"utf8": " big", "tOffsetMs": 400}, {"utf8": " world", "tOffsetMs": 900}]},
    ])
    words = parse_json3(p)
    assert [w.text for w in words] == ["hello", "big", "world"]
    assert [round(w.start, 2) for w in words] == [1.0, 1.4, 1.9]


def test_manual_caption_line_is_spread_evenly(tmp_path):
    p = _write(tmp_path, [{"tStartMs": 0, "dDurationMs": 3000, "segs": [{"utf8": "one two three"}]}])
    words = parse_json3(p)
    assert [w.text for w in words] == ["one", "two", "three"]
    assert [round(w.start, 2) for w in words] == [0.0, 1.0, 2.0]


def test_noise_tags_and_speaker_arrows_are_dropped(tmp_path):
    p = _write(tmp_path, [
        {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "[Music]"}]},
        {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": ">> hi"}, {"utf8": " there", "tOffsetMs": 300}]},
    ])
    assert [w.text for w in parse_json3(p)] == ["hi", "there"]


def test_word_ends_are_capped_and_ordered(tmp_path):
    p = _write(tmp_path, [
        {"tStartMs": 0, "dDurationMs": 10000, "segs": [{"utf8": "a"}, {"utf8": " b", "tOffsetMs": 5000}]},
    ])
    a, b = parse_json3(p)
    assert a.end <= a.start + 0.9  # a word before a long pause doesn't linger
    assert a.end <= b.start


def _write_wav(path, samples):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.astype("<i2").tobytes())


def test_read_wav_returns_float32_in_range(tmp_path):
    p = tmp_path / "a.wav"
    _write_wav(p, np.array([0, 16384, -32768, 32767]))
    audio = read_wav_16k(p)
    assert audio.dtype == np.float32
    assert audio.tolist() == pytest.approx([0.0, 0.5, -1.0, 32767 / 32768])


def test_whisper_gets_decoded_audio_not_a_path(tmp_path, monkeypatch):
    """faster-whisper's own file decoding breaks with PyAV >= 15 (removed
    `metadata_errors`), so we must hand it samples, never a filename."""
    seen = {}

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, audio, **kw):
            seen["audio"] = audio
            info = type("I", (), {"duration": 1.0})()
            word = type("W", (), {"word": " hi", "start": 0.1, "end": 0.4})()
            seg = type("S", (), {"words": [word], "end": 1.0})()
            return iter([seg]), info

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)

    def fake_ffmpeg(cmd, **kw):  # write the wav ffmpeg would have produced
        _write_wav(cmd[-1], np.zeros(16000))

    monkeypatch.setattr(source.subprocess, "run", fake_ffmpeg)
    words = source.whisper_transcribe(tmp_path / "v.mp4", tmp_path, lambda *a: None)
    assert isinstance(seen["audio"], np.ndarray)
    assert [w.text for w in words] == ["hi"]


def test_events_without_text_are_ignored(tmp_path):
    p = _write(tmp_path, [{"tStartMs": 0, "dDurationMs": 500}, {"tStartMs": 500, "segs": [{"utf8": "\n"}]}])
    assert parse_json3(p) == []


def test_manual_caption_split_over_segs_spreads_words(tmp_path):
    # Manual captions can arrive as several segs with no word offsets; every
    # word used to get the line's start time, so it was never highlighted.
    import json
    from app.source import parse_json3
    f = tmp_path / "c.json3"
    f.write_text(json.dumps({"events": [
        {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "- Joe's dish"}, {"utf8": " is a hot,"}]},
    ]}), encoding="utf-8")
    words = parse_json3(f)
    assert [w.text for w in words] == ["Joe's", "dish", "is", "a", "hot,"]  # speaker dash dropped
    starts = [w.start for w in words]
    assert len(set(starts)) == len(starts)  # every word has its own time
    assert all(w.end - w.start >= 0.2 for w in words)


def test_manual_caption_drops_multi_word_noise_tag_in_its_own_seg(tmp_path):
    from app.source import parse_json3
    f = tmp_path / "c.json3"
    f.write_text(json.dumps({"events": [
        {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "[Music playing]"}, {"utf8": " welcome back"}]},
    ]}), encoding="utf-8")
    assert [w.text for w in parse_json3(f)] == ["welcome", "back"]


def test_speaker_change_dashes_are_not_caption_words(tmp_path):
    from app.source import parse_json3
    f = tmp_path / "c.json3"
    f.write_text(json.dumps({"events": [
        {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "- Joe, – yes chef - well-done"}]},
    ]}), encoding="utf-8")
    assert [w.text for w in parse_json3(f)] == ["Joe,", "yes", "chef", "well-done"]
