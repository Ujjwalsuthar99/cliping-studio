#!/usr/bin/env python3
"""Check (and save) where a streamer's webcam is.

  .venv/bin/python check_facecam.py <video/live URL or file>            detect + preview image
  .venv/bin/python check_facecam.py <URL> --save                        remember it for this channel
  .venv/bin/python check_facecam.py <URL> --set 0,458,388,244 --save    set it by hand
  .venv/bin/python check_facecam.py <URL> --none --save                 this streamer has no webcam

Saved rects are used by cli.py and live.py for every stream of that channel.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

from clipper import facecam, ff, profiles

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("source")
p.add_argument("--set", help="x,y,w,h in source pixels")
p.add_argument("--none", action="store_true", help="mark: no webcam (use filler/blur layout)")
p.add_argument("--save", action="store_true", help="store as the confirmed facecam for this channel")
p.add_argument("--seconds", type=float, default=120, help="how much video to sample")
p.add_argument("--preview", default="facecam_preview.jpg", help="where to write the preview image")
a = p.parse_args()

def sample(src, tmp):
    """Local file -> [file]. Live URL -> one recording. VOD -> 3 short pieces across the video
    (start of streams is often a full-screen camera or a 'starting soon' screen)."""
    if not src.startswith("http"):
        return [src]
    info = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--skip-download",
                           "--print", "%(is_live)s\t%(duration)s", src], capture_output=True, text=True)
    live, _, dur = (info.stdout.strip().splitlines() or ["\t"])[0].partition("\t")
    base = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-f",
            "bv*[height<=1080]/b[height<=1080]", "--ffmpeg-location", ff.FFMPEG]
    if live == "True":
        out = os.path.join(tmp, "live.%(ext)s")
        subprocess.run(base + ["--downloader", "ffmpeg", "--downloader-args",
                               f"ffmpeg_i:-t {a.seconds:.0f}", "-o", out, src],
                       capture_output=True, text=True, timeout=900)
    else:
        d = float(dur) if dur not in ("", "NA") else 3 * a.seconds
        piece = max(30.0, a.seconds / 3)
        for frac in (0.3, 0.55, 0.8):
            t = max(0.0, d * frac)
            subprocess.run(base + ["--download-sections", f"*{t:.0f}-{t + piece:.0f}",
                                   "-o", os.path.join(tmp, f"p{int(frac * 100)}.%(ext)s"), src],
                           capture_output=True, text=True, timeout=900)
    return sorted(os.path.join(tmp, f) for f in os.listdir(tmp))


def agree(r1, r2, W, H):
    return all(abs(r1[i] - r2[i]) < (W if i % 2 == 0 else H) * 0.03 for i in range(4))


tmp = tempfile.mkdtemp()
try:
    print("sampling video…")
    files = sample(a.source, tmp)
    if not files:
        sys.exit("could not download a sample")
    W, H = ff.video_size(files[0])
    cid, name = profiles.channel(a.source)
    found = []
    if a.none:
        rect = None
    elif a.set:
        rect = tuple(int(v) for v in a.set.split(","))
    else:
        for f in files:
            r = facecam.detect(f, 0, ff.duration(f), W, H, 1080 / 672, n=24, min_ratio=0.5)
            print(f"  sample {os.path.basename(f)}: {r}")
            if r:
                found.append((r, f))
        # the rect most samples agree on
        rect, video = max(((r, f) for r, f in found),
                          key=lambda rf: sum(agree(rf[0], o, W, H) for o, _ in found),
                          default=(None, files[0]))
    video = next((f for r, f in found if r == rect), files[0])
    saved, conf = profiles.get(cid)
    print(f"channel: {name or '-'}  ({cid or 'local file'})")
    print(f"saved:    {saved} {'(confirmed)' if conf else '(auto)' if saved else ''}")
    print(f"detected: {rect}")
    if rect:
        prev = os.path.abspath(a.preview)
        profiles.preview(video, ff.duration(video) / 2, rect, prev)
        print(f"preview:  {prev}   <- open it: the red box should be exactly the webcam")
    if a.save:
        if not cid:
            sys.exit("--save needs a YouTube/Kick URL (to know the channel)")
        profiles.put(cid, name, rect if rect else [0, 0, 0, 0], confirmed=True, size=(W, H))
        print(f"saved for {name}. cli.py and live.py will use it for this channel.")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
