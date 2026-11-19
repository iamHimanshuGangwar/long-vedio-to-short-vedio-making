"""Gaming / stream layout: facecam on top, gameplay below."""
import random
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import main, render

# Measured on a real 1920x1080 stream recording: cut-out facecam bottom-left.
STREAM_CAM = (0.153, 0.676, 0.123, 0.219)


def _samples(box, n=90, hit_rate=0.5, jitter=0.02, seed=1):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        if rnd.random() < hit_rate:
            cx, cy, w, h = box
            out.append((i / 3, (cx + rnd.uniform(-jitter, jitter), cy + rnd.uniform(-jitter, jitter), w, h)))
        else:
            out.append((i / 3, None))
    return out


# ---------- detecting a facecam ----------

def test_steady_face_in_a_corner_is_a_facecam():
    cam = render.facecam_from_faces(_samples(STREAM_CAM, hit_rate=0.45))
    assert cam is not None
    assert abs(cam[0] - 0.153) < 0.02 and abs(cam[1] - 0.676) < 0.02


def test_centred_talking_head_is_not_a_facecam():
    assert render.facecam_from_faces(_samples((0.5, 0.4, 0.2, 0.35), hit_rate=0.9)) is None


def test_rare_face_is_not_a_facecam():
    assert render.facecam_from_faces(_samples(STREAM_CAM, hit_rate=0.1)) is None


def test_face_moving_around_is_not_a_facecam():
    assert render.facecam_from_faces(_samples(STREAM_CAM, hit_rate=0.9, jitter=0.25)) is None


def test_big_face_is_not_a_facecam():
    assert render.facecam_from_faces(_samples((0.2, 0.5, 0.35, 0.6), hit_rate=0.9)) is None


def test_track_samples_from_boxes_keep_the_size_filter():
    boxes = [(0.0, (0.5, 0.5, 0.2, 0.3)), (0.3, (0.1, 0.9, 0.03, 0.05)), (0.6, None)]
    assert render.track_samples(boxes) == [(0.0, 0.5), (0.3, None), (0.6, None)]


def test_track_samples_follow_the_largest_of_several_faces():
    frame = [(0.2, 0.7, 0.10, 0.2), (0.6, 0.4, 0.25, 0.4)]
    assert render.track_samples([(0.0, frame), (0.3, [])]) == [(0.0, 0.6), (0.3, None)]


def test_facecam_found_even_when_a_bigger_face_is_on_screen():
    """Reaction/gameplay streams: a big face in the content (a character, a
    video being watched) moves around while the small facecam stays put."""
    rnd = random.Random(7)
    frames = []
    for i in range(90):
        faces = [(rnd.uniform(0.35, 0.8), rnd.uniform(0.2, 0.6), 0.25, 0.4)]  # big, wandering
        if rnd.random() < 0.5:
            faces.append((0.153 + rnd.uniform(-0.01, 0.01), 0.676 + rnd.uniform(-0.01, 0.01), 0.123, 0.219))
        frames.append((i / 3, faces))
    cam = render.facecam_from_faces(frames)
    assert cam is not None
    assert abs(cam[0] - 0.153) < 0.02 and abs(cam[1] - 0.676) < 0.02


# ---------- split geometry ----------

def _check_rect(rect, src_w, src_h):
    x, y, w, h = rect
    assert all(v % 2 == 0 for v in rect), rect
    assert x >= 0 and y >= 0 and w > 0 and h > 0
    assert x + w <= src_w and y + h <= src_h


def test_split_geometry_for_stream_recording():
    g = render.split_layout(1920, 1080, STREAM_CAM, 1080, 1920)
    _check_rect(g["cam"], 1920, 1080)
    _check_rect(g["game"], 1920, 1080)
    assert g["top_h"] + g["bottom_h"] == 1920
    assert g["top_h"] % 2 == 0 and g["bottom_h"] % 2 == 0
    # crops have the same shape as the panels they fill, so nothing is stretched
    cx, cy, cw, ch = g["cam"]
    assert abs(cw / ch - 1080 / g["top_h"]) < 0.02
    gx, gy, gw, gh = g["game"]
    assert abs(gw / gh - 1080 / g["bottom_h"]) < 0.02
    # the facecam itself is inside the top crop
    face_x, face_y = 0.153 * 1920, 0.676 * 1080
    assert cx <= face_x <= cx + cw and cy <= face_y <= cy + ch
    # gameplay crop is pushed past the facecam instead of showing it twice
    assert gx >= 0.153 * 1920


def test_split_geometry_with_facecam_on_the_right():
    g = render.split_layout(1920, 1080, (0.85, 0.7, 0.12, 0.22), 1080, 1920)
    _check_rect(g["cam"], 1920, 1080)
    gx, _, gw, _ = g["game"]
    assert gx + gw <= 0.85 * 1920  # gameplay ends before the facecam


def test_no_split_when_gameplay_crop_cannot_avoid_the_facecam():
    # 4:3 source, facecam too far inward: any gameplay crop would show it again
    assert render.split_layout(1440, 1080, (0.29, 0.5, 0.2, 0.3), 1080, 1920) is None


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")
def test_render_falls_back_when_split_is_impossible(tmp_path):
    src = tmp_path / "narrow.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=960x720:rate=30",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    info = render.render_clip(src, [], 0, 2, tmp_path, "clip", layout="split", facecam=(0.29, 0.5, 0.2, 0.3))
    assert info["layout"] != "split"


def test_split_geometry_small_source():
    g = render.split_layout(854, 480, STREAM_CAM, 1080, 1920)
    _check_rect(g["cam"], 854, 480)
    _check_rect(g["game"], 854, 480)


def test_hook_can_be_placed_on_the_seam_instead_of_over_the_face():
    ass = render.build_ass([], 0, 10, "yellow", hook="Watch this", hook_top=768)
    hook_style = next(ln for ln in ass.splitlines() if ln.startswith("Style: Hook"))
    margin_v = int(hook_style.split(",")[-2])
    assert 680 <= margin_v <= 768  # the banner straddles the line between facecam and gameplay


def test_hook_position_unchanged_by_default():
    hook_style = next(ln for ln in render.build_ass([], 0, 10, "yellow", hook="x").splitlines()
                      if ln.startswith("Style: Hook"))
    assert hook_style.endswith(",90,90,230,1")


# ---------- API ----------

def test_api_accepts_split_layout(monkeypatch):
    monkeypatch.setattr(main._pool, "submit", lambda *a: None)
    monkeypatch.setattr(main, "_jobs", {})
    res = TestClient(main.app).post("/api/jobs", json={"url": "https://youtu.be/x", "layout": "split"})
    assert res.status_code == 200 and res.json()["options"]["layout"] == "split"


# ---------- real render ----------

@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")
def test_real_split_render_is_vertical(tmp_path):
    src = tmp_path / "stream.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=1280x720:rate=30",
                    "-f", "lavfi", "-i", "sine=duration=3", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    str(src)], check=True)
    info = render.render_clip(src, [], 0, 3, tmp_path, "clip", layout="split", facecam=STREAM_CAM)
    assert info["layout"] == "split"
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(tmp_path / "clip.mp4")], capture_output=True, text=True).stdout.strip()
    assert out == "1080,1920"
