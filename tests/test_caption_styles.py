from app import render
from app.source import Word


def test_build_up_style_shows_only_words_spoken_so_far():
    words = [Word("the", 0, 0.3), Word("first", 0.3, 0.6), Word("time", 0.6, 0.9)]
    caps = [ln.split(",,", 1)[1] for ln in render.build_ass(words, 0, 5, "reveal", None).splitlines()
            if ln.startswith("Dialogue: 0,")]
    assert len(caps) == 3
    assert "first" not in caps[0] and "time" not in caps[0]
    assert "time" not in caps[1] and "the" in caps[1]
    assert "the" in caps[2] and "first" in caps[2] and "time" in caps[2]


def test_other_styles_still_show_the_whole_group():
    words = [Word("the", 0, 0.3), Word("first", 0.3, 0.6), Word("time", 0.6, 0.9)]
    first = next(ln for ln in render.build_ass(words, 0, 5, "yellow", None).splitlines() if ln.startswith("Dialogue: 0,"))
    assert "TIME" in first
