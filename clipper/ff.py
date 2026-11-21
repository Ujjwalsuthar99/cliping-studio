from __future__ import annotations
import re
import subprocess
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def run(args):
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + args,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + p.stderr[-1500:])
    return p


def duration(path: str) -> float:
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
    if not m:
        raise RuntimeError("cannot read duration of " + path)
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def video_size(path: str):
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", p.stderr)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
