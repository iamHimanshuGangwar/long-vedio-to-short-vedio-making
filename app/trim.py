"""Make clips tighter by cutting long pauses and "uh"/"um" (issue #1).

- Pauses are measured from the audio with ffmpeg's silencedetect. Caption
  timings can't be used for this: human-written YouTube captions spread each
  line's words evenly, so their gaps aren't real pauses. In gaming streams the
  game keeps making sound during speech pauses, so those aren't cut.
- Fillers come from the transcript (YouTube's automatic captions and Whisper
  include "uh"/"um"). Their timings are rough, so a filler is only cut when it
  stands on its own: long enough to be a real timing and not overlapping the
  words around it. "like" is never cut; it's usually a real word.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .source import Word

FILLERS = {"uh", "um", "uhm", "umm", "erm", "er"}
NOISE_DB = -35       # quieter than this counts as silence
MIN_PAUSE = 0.6      # only pauses at least this long are worth cutting
PAD = 0.15           # breathing room kept on each side of a cut
MIN_FILLER = 0.1     # shorter filler timings are too uncertain to cut
MIN_KEEP = 0.5       # wordless pieces shorter than this between cuts are dropped

_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def parse_silences(stderr: str, clip_len: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    start = None
    for line in stderr.splitlines():
        m = _START.search(line)
        if m:
            try:
                start = max(0.0, float(m.group(1)))
            except ValueError:
                start = None
            continue
        m = _END.search(line)
        if m and start is not None:
            try:
                out.append((start, min(clip_len, float(m.group(1)))))
            except ValueError:
                pass
            start = None
    if start is not None and start < clip_len:
        out.append((start, clip_len))  # silence runs to the end of the clip
    return out


def detect_silences(video: Path, start: float, end: float) -> list[tuple[float, float]]:
    """Silences in [start, end) of the video, in seconds from the clip's start."""
    clip_len = end - start
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{clip_len:.3f}", "-i", str(video),
         "-vn", "-af", f"silencedetect=n={NOISE_DB}dB:d={MIN_PAUSE}", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return parse_silences(proc.stderr, clip_len)


def filler_spans(words: list[Word], clip_start: float) -> list[tuple[float, float]]:
    """Cleanly-timed "uh"/"um" words, in seconds from the clip's start."""
    spans = []
    for i, w in enumerate(words):
        if w.text.lower().strip(".,!?;:") not in FILLERS:
            continue
        if w.end - w.start < MIN_FILLER:
            continue
        prev_end = words[i - 1].end if i > 0 else float("-inf")
        next_start = words[i + 1].start if i + 1 < len(words) else float("inf")
        if prev_end > w.start or next_start < w.end:
            continue  # overlaps a neighbour: cutting it could clip real speech
        spans.append((round(w.start - clip_start, 3), round(w.end - clip_start, 3)))
    return spans


def keep_segments(
    clip_len: float, cuts: list[tuple[float, float]], pad: float = PAD, min_cut: float = 0.0
) -> list[tuple[float, float]]:
    """The parts of [0, clip_len] to keep. Each cut is shrunk by `pad` on both
    sides so speech isn't clipped; cuts shorter than `min_cut` are ignored."""
    shrunk = []
    for s, e in sorted(cuts):
        s, e = max(0.0, s), min(clip_len, e)
        if e - s < min_cut:
            continue
        s2 = s if s <= 0 else s + pad
        e2 = e if e >= clip_len else e - pad
        if e2 > s2:
            shrunk.append([s2, e2])
    merged: list[list[float]] = []
    for s, e in shrunk:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    keep, pos = [], 0.0
    for s, e in merged:
        if s > pos:
            keep.append((round(pos, 3), round(s, 3)))
        pos = max(pos, e)
    if pos < clip_len:
        keep.append((round(pos, 3), round(clip_len, 3)))
    return keep


def remap(t: float, keep: list[tuple[float, float]]) -> float | None:
    """Clip time -> time in the shortened clip, or None if it was cut."""
    removed = 0.0
    prev_end = 0.0
    for s, e in keep:
        removed += s - prev_end
        if s <= t <= e:
            return round(t - removed, 3)
        prev_end = e
    return None


def remap_words(words: list[Word], clip_start: float, keep: list[tuple[float, float]]) -> list[Word]:
    """Words re-timed onto the shortened clip (times from its start).

    A word is judged by its whole span, not just its start: the part of it that
    falls in kept footage is captioned (a word starting before the clip, or
    inside a cut, still gets its caption where it's heard). Words entirely
    inside a cut are dropped."""
    out = []
    for w in words:
        a, b = w.start - clip_start, w.end - clip_start
        parts = [(max(a, s), min(b, e)) for s, e in keep if a < e and b > s]
        if not parts:
            continue
        s, e = remap(parts[0][0], keep), remap(parts[-1][1], keep)
        out.append(Word(w.text, s, max(e, s + 0.05)))
    return out


def select_expr(keep: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keep)


def plan(video: Path, words: list[Word], start: float, end: float) -> list[tuple[float, float]] | None:
    """Keep segments for a clip, or None when there's nothing worth cutting."""
    clip_len = end - start
    clip_words = [w for w in words if w.end > start and w.start < end]
    cuts = detect_silences(video, start, end) + filler_spans(clip_words, start)
    keep = keep_segments(clip_len, cuts, min_cut=0.0)
    # A scrap under MIN_KEEP between two cuts, with nobody speaking in it, would
    # only flash on screen between two jump cuts: drop it.
    spans = [(w.start - start, w.end - start) for w in clip_words]
    keep = [(s, e) for s, e in keep
            if e - s >= MIN_KEEP or s == 0.0 or e >= round(clip_len, 3)
            or any(ws < e and we > s for ws, we in spans)]  # any word overlapping it, not just starting in it
    if keep == [(0.0, round(clip_len, 3))] or sum(e - s for s, e in keep) < 0.5 * clip_len:
        return None  # nothing to cut, or so much that it's probably a music/quiet clip: leave it alone
    return keep
