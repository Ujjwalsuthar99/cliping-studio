"""Command-line options shared by cli.py (videos) and live.py (live streams)."""
from __future__ import annotations
import argparse
from .config import Config


def add_common(p: argparse.ArgumentParser):
    p.add_argument("--min", type=float, default=30, help="min clip length (s)")
    p.add_argument("--max", type=float, default=45, help="max clip length (s)")
    p.add_argument("--max-mb", type=float, default=50)
    p.add_argument("--model", default="small", help="whisper model: tiny/base/small/medium")
    p.add_argument("--lang", default=None, help="force language, e.g. hi or en")
    p.add_argument("--device", choices=["cpu", "mps"], default="cpu",
                   help="mps = Apple GPU (experimental, slower on M1 Pro in tests)")
    p.add_argument("--compute-type", default=None, help="default: int8 on cpu, float16 on mps")
    p.add_argument("--layout", choices=["auto", "facecam", "filler", "blur", "crop"], default="auto",
                   help="auto: facecam split if webcam found, else filler split, else blur")
    p.add_argument("--facecam", default=None, help="manual webcam rect in source pixels: x,y,w,h")
    p.add_argument("--filler-dir", default="assets/filler", help="folder of background videos")
    p.add_argument("--captions", action="store_true", help="burn in Whisper captions (off by default)")
    p.add_argument("--sticker", default="assets/stickers/like_subscribe.png",
                   help="transparent PNG overlaid in each clip (default: like & subscribe)")
    p.add_argument("--no-sticker", action="store_true")


def make_config(a, **extra) -> Config:
    return Config(min_len=a.min, max_len=a.max, target_len=(a.min + a.max) / 2,
                  max_size_mb=a.max_mb, whisper_model=a.model, language=a.lang,
                  layout=a.layout, device=a.device, filler_dir=a.filler_dir,
                  compute_type=a.compute_type or ("float16" if a.device == "mps" else "int8"),
                  facecam_rect=tuple(int(v) for v in a.facecam.split(",")) if a.facecam else None,
                  captions=a.captions, sticker=None if a.no_sticker else a.sticker, **extra)
