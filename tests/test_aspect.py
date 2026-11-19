import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import main, render
from app.source import Word

YT = "https://www.youtube.com/watch?v=abc123"


def _playres(ass):
    x = int(re.search(r"PlayResX: (\d+)", ass).group(1))
    y = int(re.search(r"PlayResY: (\d+)", ass).group(1))
    return x, y


def _style(ass, name):
    line = next(ln for ln in ass.splitlines() if ln.startswith(f"Style: {name},"))
    f = line.split(",")
    return {"size": int(f[2]), "ml": int(f[19]), "mr": int(f[20]), "mv": int(f[21])}


WORDS = [Word("hello", 0, 0.4), Word("there", 0.4, 0.8)]


def test_aspects_table():
    assert render.ASPECTS == {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}


def test_build_ass_default_is_vertical_and_unchanged():
    ass = render.build_ass(WORDS, clip_start=0, clip_len=5, style_key="yellow", hook="hi")
    assert _playres(ass) == (1080, 1920)
    assert "Style: Cap,Arial Black,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,2,70,70,470,1" in ass
    assert "Style: Hook,Arial Black,64,&H00101010,&H00101010,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,230,1" in ass


@pytest.mark.parametrize("aspect", ["9:16", "1:1", "16:9"])
def test_build_ass_playres_and_margins(aspect):
    w, h = render.ASPECTS[aspect]
    ass = render.build_ass(WORDS, 0, 5, "yellow", "hi", size=(w, h))
    assert _playres(ass) == (w, h)
    cap, hook = _style(ass, "Cap"), _style(ass, "Hook")
    # captions sit in the lower part of the frame, hook near the top, all inside the frame
    assert 0.1 * h <= cap["mv"] <= 0.3 * h
    assert hook["mv"] < 0.15 * h
    assert cap["ml"] + cap["mr"] < w / 2
    assert 40 <= cap["size"] <= 110


def test_build_ass_square_and_landscape_use_smaller_bottom_margin():
    v = _style(render.build_ass(WORDS, 0, 5, "yellow", None), "Cap")["mv"]
    sq = _style(render.build_ass(WORDS, 0, 5, "yellow", None, size=(1080, 1080)), "Cap")["mv"]
    ls = _style(render.build_ass(WORDS, 0, 5, "yellow", None, size=(1920, 1080)), "Cap")["mv"]
    assert sq < v and ls < v


@pytest.mark.parametrize(
    "src,out,expected",
    [
        ((1920, 1080), (1080, 1920), 606),   # existing behaviour: int(1080*9/16)//2*2
        ((1920, 1080), (1080, 1080), 1080),
        ((1920, 1080), (1920, 1080), 1920),
        ((1280, 720), (1080, 1080), 720),
        ((1080, 1920), (1920, 1080), 1080),  # clamped to source width
    ],
)
def test_crop_width(src, out, expected):
    assert render.crop_width(*src, *out) == expected


def test_crop_x_expr_signature_unchanged():
    assert render._crop_x_expr([(0.0, 0.5)], src_w=1920, crop_w=1080) == "420"


@pytest.fixture
def client(monkeypatch):
    submitted = []
    monkeypatch.setattr(main._pool, "submit", lambda fn, *a: submitted.append(a))
    monkeypatch.setattr(main, "_jobs", {})
    return TestClient(main.app)


def test_api_default_aspect_is_vertical(client):
    res = client.post("/api/jobs", json={"url": YT})
    assert res.status_code == 200 and res.json()["options"]["aspect"] == "9:16"


@pytest.mark.parametrize("aspect", ["9:16", "1:1", "16:9"])
def test_api_accepts_aspects(client, aspect):
    res = client.post("/api/jobs", json={"url": YT, "aspect": aspect})
    assert res.status_code == 200 and res.json()["options"]["aspect"] == aspect


def test_api_rejects_unknown_aspect(client):
    res = client.post("/api/jobs", json={"url": YT, "aspect": "4:5"})
    assert res.status_code == 400 and "Unknown aspect ratio." in res.text


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
def test_real_render_dimensions(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=1280x720:rate=30",
         "-f", "lavfi", "-i", "sine=duration=3", "-shortest", "-c:v", "libx264", "-preset", "ultrafast", str(src)],
        check=True,
    )
    words = [Word("one", 0.2, 0.6), Word("two", 0.6, 1.0), Word("three.", 1.0, 1.5)]
    for aspect, (w, h) in render.ASPECTS.items():
        for layout in ("fit", "fill"):
            name = f"o_{aspect.replace(':', 'x')}_{layout}"
            render.render_clip(src, words, 0, 2, tmp_path, name, layout=layout, hook="test", aspect=aspect)
            assert render._probe_size(tmp_path / f"{name}.mp4") == (w, h)
