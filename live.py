#!/usr/bin/env python3
"""Watch one or more live streams and auto-clip highlights into 30-45s Shorts while they run.

  .venv/bin/python live.py https://www.youtube.com/@SomeChannel/live https://kick.com/someone
"""
import argparse
import os
import signal
import subprocess
import sys
import threading
from clipper.cliargs import add_common, make_config
from clipper.live import LiveClipper

p = argparse.ArgumentParser(description="Live-stream auto clipper")
p.add_argument("urls", nargs="+", help="live URLs: YouTube watch/@channel/live, Kick channel")
p.add_argument("-o", "--out", default="live_output")
p.add_argument("--min-score", type=float, default=0.8,
               help="score that triggers an immediate clip (strong moments)")
p.add_argument("--per-hour", type=int, default=8,
               help="target/max clips per stream per hour: if 60/N minutes pass with no strong "
                    "moment, the best moment of that stretch is clipped")
p.add_argument("--floor", type=float, default=0.3,
               help="weakest score allowed for the steady per-block clip (0 = always clip)")
p.add_argument("--warmup", type=float, default=180,
               help="seconds to record before clipping starts (learns the stream's normal volume)")
u = p.add_argument_group("auto upload (runs upload.py alongside, see upload_config.json)")
u.add_argument("--upload", action="store_true", help="upload new clips to YouTube as they are cut")
u.add_argument("--upload-privacy", choices=["private", "unlisted", "public", "scheduled"],
               default=None, help="default: privacy from upload_config.json")
u.add_argument("--upload-min-score", type=float, default=None,
               help="only upload clips scoring at least this (default from upload_config.json)")
u.add_argument("--upload-every", type=float, default=0,
               help="minutes between uploads (0 = upload each clip as soon as it is cut)")
u.add_argument("--upload-max-per-day", type=int, default=None)
add_common(p)
a = p.parse_args()
cfg = make_config(a)
uploader = None
if a.upload:
    root = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, "-u", os.path.join(root, "upload.py"), "run", "--watch", "--new-only",
           "--every", str(a.upload_every), "--clip-dirs", os.path.relpath(os.path.abspath(a.out), root)]
    for flag, val in (("--privacy", a.upload_privacy), ("--min-score", a.upload_min_score),
                      ("--max-per-day", a.upload_max_per_day)):
        if val is not None:
            cmd += [flag, str(val)]
    uploader = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)

    def _relay():
        for line in uploader.stdout:
            print(f"[upload] {line.rstrip()}", flush=True)
        print(f"[upload] uploader stopped (exit {uploader.wait()})", flush=True)
    threading.Thread(target=_relay, daemon=True).start()

lc = LiveClipper(a.urls, a.out, cfg, a.min_score, a.per_hour, a.warmup, a.floor)
signal.signal(signal.SIGTERM, lambda *_: lc.stop_flag.set())  # `kill <pid>` stops cleanly
try:
    lc.run()
finally:
    if uploader and uploader.poll() is None:
        uploader.terminate()
