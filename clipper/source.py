from __future__ import annotations
import os
import re
import subprocess
import sys
from . import ff


def parse_time(s: str) -> float:
    """'1:02:03', '62:03', '3723' or '3723.5' -> seconds."""
    parts = s.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(f"bad time '{s}' (use H:MM:SS, MM:SS or seconds)")
    v = 0.0
    for p in parts:
        v = v * 60 + float(p)
    return v


def parse_section(s: str):
    """'1:00:00-1:20:00' -> (3600.0, 4800.0)"""
    if "-" not in s:
        raise ValueError(f"bad section '{s}' (use START-END, e.g. 1:00:00-1:20:00)")
    a, b = s.split("-", 1)
    a, b = parse_time(a), parse_time(b)
    if b <= a:
        raise ValueError(f"section '{s}' ends before it starts")
    return a, b


def _hms(t: float) -> str:
    return f"{int(t // 3600)}h{int(t % 3600 // 60):02d}m{int(t % 60):02d}s"


def fetch(src: str, workdir: str, section=None) -> str:
    """Return a local video path. Downloads with yt-dlp if src is a URL.

    section=(start, end) seconds: download (URL) or cut (file) only that part.
    """
    os.makedirs(workdir, exist_ok=True)
    if os.path.exists(src):
        src = os.path.abspath(src)
        if section is None:
            return src
        a, b = section
        out = os.path.join(workdir, f"{os.path.splitext(os.path.basename(src))[0]}"
                                    f"_{_hms(a)}-{_hms(b)}.mp4")
        if not os.path.exists(out):  # stream copy: fast, no re-encode
            ff.run(["-ss", f"{a:.2f}", "-i", src, "-t", f"{b - a:.2f}", "-map", "0:v:0",
                    "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero", out])
        return out
    if not src.startswith(("http://", "https://")):
        raise FileNotFoundError(src)
    name = "%(id)s" + (f"_{_hms(section[0])}-{_hms(section[1])}" if section else "")
    out = os.path.join(workdir, name + ".%(ext)s")
    cmd = [sys.executable, "-m", "yt_dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
           "--merge-output-format", "mp4", "-o", out, "--print", "after_move:filepath",
           "--no-simulate", "--no-playlist", "--ffmpeg-location", ff.FFMPEG, src]
    if section:
        cmd[3:3] = ["--download-sections", f"*{section[0]:.0f}-{section[1]:.0f}"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("yt-dlp failed: " + p.stderr[-1000:])
    return p.stdout.strip().splitlines()[-1]
