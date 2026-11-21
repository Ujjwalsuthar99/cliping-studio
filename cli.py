#!/usr/bin/env python3
"""Clip a finished video (file or URL) into 30-45s Shorts."""
import argparse
from clipper.cliargs import add_common, make_config
from clipper.pipeline import process

p = argparse.ArgumentParser(description="Auto-clip highlights into 30-45s Shorts-ready clips")
p.add_argument("source", help="video file path or URL (YouTube/Kick VOD)")
p.add_argument("-o", "--out", default="output")
p.add_argument("--max-clips", type=int, default=10)
p.add_argument("--full-transcribe", action="store_true",
               help="transcribe the whole video (slow) instead of only loud candidate stretches")
p.add_argument("--section", action="append", default=[], metavar="START-END",
               help="only download/search this part, e.g. 1:00:00-1:20:00 (repeatable; "
                    "--max-clips applies per section)")
u = p.add_argument_group("auto upload (see upload_config.json)")
u.add_argument("--upload", action="store_true", help="upload this run's clips to YouTube when done")
u.add_argument("--upload-privacy", choices=["private", "unlisted", "public", "scheduled"], default=None)
u.add_argument("--upload-min-score", type=float, default=None)
u.add_argument("--upload-every", type=float, default=0,
               help="minutes between uploads (0 = all at once; with 'scheduled' = publish spacing)")
u.add_argument("--upload-max-per-day", type=int, default=None)
add_common(p)
a = p.parse_args()
from clipper.source import parse_section  # noqa: E402
try:
    sections = [parse_section(s) for s in a.section] or None
except ValueError as e:
    p.error(str(e))
import os, subprocess, sys, time  # noqa: E401,E402
started = time.time()
process(a.source, a.out, make_config(a, max_clips=a.max_clips, full_transcribe=a.full_transcribe),
        sections=sections)
if a.upload:
    root = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, "-u", os.path.join(root, "upload.py"), "run", "--watch", "--until-empty",
           "--since", str(started - 1), "--every", str(a.upload_every),
           "--clip-dirs", os.path.relpath(os.path.abspath(a.out), root)]
    for flag, val in (("--privacy", a.upload_privacy), ("--min-score", a.upload_min_score),
                      ("--max-per-day", a.upload_max_per_day)):
        if val is not None:
            cmd += [flag, str(val)]
    print("uploading…")
    sys.exit(subprocess.call(cmd, cwd=root))
