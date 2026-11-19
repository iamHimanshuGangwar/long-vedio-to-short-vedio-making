import pytest

from app import selector
from app.selector import Clip, Sentence


def test_sentences_break_on_punctuation(words_factory):
    s = selector.to_sentences(words_factory("I love this. Do you? Yes"))
    assert [x.text for x in s] == ["I love this.", "Do you?", "Yes"]


def test_sentences_break_on_pause(words_factory):
    s = selector.to_sentences(words_factory("no punctuation here | after the pause"))
    assert [x.text for x in s] == ["no punctuation here", "after the pause"]


def test_sentences_are_capped_at_18_words(words_factory):
    s = selector.to_sentences(words_factory(" ".join(f"w{i}" for i in range(40))))
    assert [len(x.text.split()) for x in s] == [18, 18, 4]


def _sents(n, length=5.0):
    return [Sentence(i * length, (i + 1) * length - 0.1, f"line {i}") for i in range(n)]


def test_fit_trims_overlong_clip():
    a, b = selector._fit(_sents(30), 0, 20, 30, 60)
    assert _sents(30)[b].end - _sents(30)[a].start <= 60


def test_fit_extends_short_clip():
    s = _sents(30)
    a, b = selector._fit(s, 0, 1, 30, 60)
    assert s[b].end - s[a].start >= 30


def test_dedupe_keeps_best_of_overlapping():
    clips = [
        Clip(0, 40, "a", "", 70, "", ""),
        Clip(20, 60, "b", "", 90, "", ""),
        Clip(100, 140, "c", "", 50, "", ""),
    ]
    assert [c.title for c in selector._dedupe(clips)] == ["b", "c"]


@pytest.mark.parametrize("raw, expected", [
    ("Stress breaks your brain.", "Stress breaks your brain."),
    ("Did you know that for every single surgeon who operates, there are 49?", "Did you know that for every single..."),
    ('"Quoted hook"', "Quoted hook"),
    ("First sentence. Second sentence.", "First sentence."),
])
def test_short_hook(raw, expected):
    assert selector._short_hook(raw) == expected


def test_heuristic_returns_non_overlapping_clips_in_range(words_factory):
    text = " ".join(f"Why is the secret number {i} so important? | Because it is." for i in range(80))
    sents = selector.to_sentences(words_factory(text))
    clips = selector.select_heuristic(sents, 4, 30, 60)
    assert 1 <= len(clips) <= 4
    for c in clips:
        assert 30 <= c.end - c.start <= 60
        assert 0 <= c.score <= 100
    ordered = sorted(clips, key=lambda c: c.start)
    assert all(x.end <= y.start for x, y in zip(ordered, ordered[1:]))


def test_heuristic_with_too_little_speech_returns_nothing(words_factory):
    assert selector.select_heuristic(selector.to_sentences(words_factory("too short")), 3, 30, 60) == []


def test_select_without_ai_uses_basic(words_factory, no_ai):
    text = " ".join(f"Here is fact number {i}. | " for i in range(120))
    clips, method, warning = selector.select(selector.to_sentences(words_factory(text)), "t", 2, 30, 60)
    assert method == "basic" and warning is None and clips


def test_select_falls_back_when_ai_fails(words_factory, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")

    def boom(*a, **k):
        raise RuntimeError("API key not valid")

    monkeypatch.setattr(selector, "select_with_ai", boom)
    text = " ".join(f"Here is fact number {i}. | " for i in range(120))
    clips, method, warning = selector.select(selector.to_sentences(words_factory(text)), "t", 2, 30, 60)
    assert method == "basic" and clips
    assert "rejected" in warning
