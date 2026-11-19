from app import render
from app.source import Word


def test_camera_holds_still_while_face_stays_in_deadzone():
    samples = [(i / 3, 0.5 + (0.02 if i % 2 else -0.02)) for i in range(30)]
    assert len(render.camera_path(samples)) == 1


def test_camera_moves_when_face_clearly_leaves():
    samples = [(i / 3, 0.3) for i in range(15)] + [(5 + i / 3, 0.75) for i in range(15)]
    keys = render.camera_path(samples)
    assert len(keys) == 2
    assert abs(keys[-1][1] - 0.75) < 0.01


def test_camera_ignores_one_frame_flicker():
    samples = [(i / 3, 0.5) for i in range(30)]
    samples[10] = (10 / 3, 0.9)
    assert len(render.camera_path(samples)) == 1


def test_camera_gives_up_when_faces_are_rare():
    samples = [(i / 3, None) for i in range(30)]
    samples[0] = (0, 0.5)
    assert render.camera_path(samples) is None


def test_crop_expression_is_clamped_to_frame():
    expr = render._crop_x_expr([(0.0, 0.0), (2.0, 1.0)], src_w=1920, crop_w=608)
    assert expr == "if(lt(t,2.00),0,1312)"


def test_ass_highlights_one_word_at_a_time():
    words = [Word("hello", 10.0, 10.4), Word("there", 10.4, 10.8), Word("friend", 10.8, 11.2)]
    ass = render.build_ass(words, clip_start=10.0, clip_len=5.0, style_key="yellow", hook=None)
    lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue")]
    assert len(lines) == 3
    assert lines[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.40,Cap")
    assert "HELLO" in lines[0] and "THERE" in lines[0]  # yellow style is upper-case


def test_ass_adds_hook_banner():
    ass = render.build_ass([Word("hi", 0, 0.5)], 0, 10, "clean", hook="Watch this")
    assert "Hook,,0,0,0,,{\\fad(120,250)}WATCH THIS" in ass


def test_ass_escapes_override_braces():
    ass = render.build_ass([Word("{evil}", 0, 0.5)], 0, 10, "clean", hook=None)
    assert "{evil}" not in ass


def test_caption_groups_split_on_pause_and_punctuation():
    words = [Word("one", 0, 0.3), Word("two.", 0.3, 0.6), Word("three", 0.6, 0.9), Word("four", 2.0, 2.3)]
    groups = render._groups(words)
    assert [[w.text for w in g] for g in groups] == [["one", "two."], ["three"], ["four"]]
