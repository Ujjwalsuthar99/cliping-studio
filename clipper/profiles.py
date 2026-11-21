"""Per-streamer facecam memory (facecams.json).

Every streamer places the webcam differently, but each one keeps it in the same spot from
stream to stream. So the rect is stored per channel: confirmed rects (set with
check_facecam.py --save) are always used; auto-detected ones are reused and a preview image is
written next to the clips so a wrong one is easy to spot and fix.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from . import ff

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "facecams.json")
_cache = {}


def channel(url: str):
    """(channel_id, channel_name) for a YouTube/Kick URL, or (None, None) for local files."""
    if not url or not url.startswith("http"):
        return None, None
    if url not in _cache:
        p = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--skip-download",
                            "--no-playlist", "--print", "%(channel_id)s\t%(channel)s", url],
                           capture_output=True, text=True)
        cid, _, name = ((p.stdout.strip().splitlines() or [""])[0]).partition("\t")
        _cache[url] = (cid if cid not in ("", "NA") else None, name if name not in ("", "NA") else None)
    return _cache[url]


def _load():
    if os.path.exists(PATH):
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


NONE = "none"  # saved marker: this streamer has no webcam


def get(cid, size=None):
    """-> (rect | NONE | None, confirmed). Rect is rescaled if the video size differs."""
    e = _load().get(cid) if cid else None
    if not e:
        return None, False
    r = e["rect"]
    if not r or r[2] <= 0:
        return NONE, bool(e.get("confirmed"))
    ss = e.get("source_size")
    if size and ss and tuple(ss) != tuple(size):
        k = size[0] / ss[0]
        r = [int(v * k) // 2 * 2 for v in r]
    return tuple(r), bool(e.get("confirmed"))


def put(cid, name, rect, confirmed=False, size=None):
    if not cid:
        return
    d = _load()
    if d.get(cid, {}).get("confirmed") and not confirmed:
        return  # never overwrite a rect the user confirmed
    d[cid] = {"name": name, "rect": list(rect), "confirmed": confirmed,
              "source_size": list(size) if size else None}
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PATH)


def preview(video: str, t: float, rect, out: str):
    """Save a frame with the facecam rect drawn in red (for a quick visual check)."""
    x, y, w, h = rect
    ff.run(["-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
            "-vf", f"drawbox={x}:{y}:{w}:{h}:red:6,scale=960:-2", out])
