from pathlib import Path

from app import main
from app.source import Word

ROOT = Path(__file__).resolve().parent.parent
WORDS = [Word("a", 10.0, 10.4), Word("b", 20.0, 20.4), Word("later", 30.0, 30.4)]


def test_pads_around_clip_without_range():
    start, end = main._render_bounds(10.0, 20.4, WORDS, duration=100)
    assert start == 9.85
    assert end == 21.9  # runs on into the pause, capped at 1.5 s


def test_tail_stops_before_next_word():
    words = [Word("a", 10.0, 10.4), Word("b", 20.0, 20.4), Word("next", 20.8, 21.0)]
    _, end = main._render_bounds(10.0, 20.4, words, duration=100)
    assert end == 20.7


def test_never_past_end_of_video():
    _, end = main._render_bounds(10.0, 20.4, WORDS[:2], duration=20.6)
    assert end == 20.6


def test_padding_is_clamped_to_requested_range():
    start, end = main._render_bounds(10.0, 20.4, WORDS, duration=100, range_start=10.0, range_end=21.0)
    assert start == 10.0
    assert end == 21.0


def test_time_inputs_allow_typing_a_colon_on_phones():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for field in ('id="from"', 'id="to"'):
        line = next(ln for ln in html.splitlines() if field in ln)
        assert 'inputmode="numeric"' not in line  # numeric keypads have no ':' key
