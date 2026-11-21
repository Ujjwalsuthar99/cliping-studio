from __future__ import annotations
import os
import random
from typing import Optional
from . import ff
from .config import Config

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm")


def _ts(t: float) -> str:
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_ass(words, start: float, path: str, cfg: Config, seam_y: Optional[int] = None):
    """Shorts-style captions, 3 words per line. Centered on seam_y if given, else lower third."""
    if seam_y is None:
        align, margin = 2, 420                      # bottom-center
    else:
        align, margin = 8, max(0, seam_y - 50)      # top-center, pushed down to the seam
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {cfg.width}\nPlayResY: {cfg.height}\n\n"
            "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,"
            "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,"
            "Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            f"Style: Default,Arial,78,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,6,0,"
            f"{align},60,60,{margin},1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    lines = []
    for i in range(0, len(words), 3):
        grp = words[i:i + 3]
        s = max(0.0, grp[0][0] - start)
        e = max(s + 0.2, grp[-1][1] - start)
        txt = " ".join(w[2].strip() for w in grp).upper()
        txt = txt.replace("\n", " ").replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Default,,0,0,0,,{txt}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")


def list_fillers(cfg: Config):
    d = cfg.filler_dir
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if f.lower().endswith(VIDEO_EXT) and not f.startswith("."))


def _fill(label_in: str, w: int, h: int, label_out: str, fps: int) -> str:
    return (f"{label_in}scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps={fps}{label_out}")


def _graph(cfg: Config, layout: str, cam=None):
    """Return (filter_graph, out_label, seam_y)."""
    W, H, R = cfg.width, cfg.height, cfg.fps
    if layout == "facecam":
        x, y, w, h = cam
        th = int(H * cfg.facecam_frac) // 2 * 2
        # whole webcam, fitted (not cropped) into the panel; blurred copy fills the sides.
        # lanczos + light sharpening keep a small webcam from looking smeared when enlarged.
        g = (f"[0:v]split[g0][c0];[c0]crop={w}:{h}:{x}:{y},split[cb][cf];"
             f"[cb]scale={W}:{th}:force_original_aspect_ratio=increase,crop={W}:{th},"
             f"gblur=sigma=25,eq=brightness=-0.08[cbg];"
             f"[cf]scale={W}:{th}:force_original_aspect_ratio=decrease:flags=lanczos,"
             f"unsharp=5:5:0.6[cfg];"
             f"[cbg][cfg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={R}[cam];"
             + _fill("[g0]", W, H - th, "[game]", R) + ";[cam][game]vstack[v]")
        return g, "[v]", th
    if layout == "filler":
        hh = H // 2
        g = (_fill("[0:v]", W, hh, "[game]", R) + ";" + _fill("[1:v]", W, H - hh, "[fil]", R)
             + ";[game][fil]vstack[v]")
        return g, "[v]", hh
    if layout == "crop":
        return _fill("[0:v]", W, H, "[v]", R), "[v]", None
    # blur
    g = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,"
         f"crop={W}:{H},gblur=sigma=30[bg];"
         f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
         f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={R}[v]")
    return g, "[v]", None


def _sticker_graph(cfg: Config, idx: int, dur: float, seam, base: str, out: str) -> str:
    """Overlay the sticker a few times per clip, with a fade and a short slide-up pop."""
    W, H = cfg.width, cfg.height
    sw = int(W * cfg.sticker_width) // 2 * 2
    shows = [(a if a >= 0 else dur + a, d) for a, d in cfg.sticker_times]
    shows = [(a, min(d, dur - a)) for a, d in shows if 0 <= a < dur - 0.5]
    if not shows:
        return f"{base}null{out}"
    cy = seam if seam is not None else int(H * 0.72)  # centre line for the sticker
    labels = [f"[st{i}]" for i in range(len(shows))]
    g = f"[{idx}:v]format=rgba,scale={sw}:-2,split={len(shows)}" + "".join(labels) + ";"
    cur = base
    for i, (a, d) in enumerate(shows):
        fo = max(a, a + d - 0.3)
        g += (f"{labels[i]}fade=in:st={a:.2f}:d=0.3:alpha=1,"
              f"fade=out:st={fo:.2f}:d=0.3:alpha=1[sf{i}];")
        nxt = out if i == len(shows) - 1 else f"[so{i}]"
        pop = f"40*max(0\\,1-(t-{a:.2f})/0.3)"
        g += (f"{cur}[sf{i}]overlay=x=(W-w)/2:y={cy}-h/2+{pop}:"
              f"enable='between(t,{a:.2f},{a + d:.2f})'{nxt}")
        g += ";" if nxt != out else ""
        cur = nxt
    return g


def export_clip(video: str, start: float, end: float, words, out: str, cfg: Config,
                layout: str = "blur", cam=None, filler: Optional[str] = None) -> int:
    dur = end - start
    graph, label, seam = _graph(cfg, layout, cam)
    inputs = ["-ss", f"{start:.2f}", "-i", video]
    if layout == "filler":
        fdur = ff.duration(filler)
        off = random.uniform(0, max(0.0, fdur - dur))
        inputs += ["-stream_loop", "-1", "-ss", f"{off:.2f}", "-i", filler]
    ass = None
    if cfg.captions and words:
        ass = out[:-4] + ".ass"
        write_ass(words, start, ass, cfg, seam)
        esc = ass.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        graph = graph[: -len(label)] + "[pre];" + f"[pre]subtitles='{esc}'{label}"
    if cfg.sticker and os.path.exists(cfg.sticker):
        idx = inputs.count("-i")
        inputs += ["-loop", "1", "-framerate", str(cfg.fps), "-i", cfg.sticker]
        graph = graph[: -len(label)] + "[pre2];" + _sticker_graph(cfg, idx, dur, seam, "[pre2]", label)
    cap = cfg.max_size_mb * 1024 * 1024
    vb = int(cap * 8 * 0.92 / dur - 128_000)  # video bitrate that fits the size cap
    try:
        for rate in sorted({min(vb, r) for r in (8_000_000, 5_000_000, 3_000_000, 1_500_000)},
                           reverse=True):
            ff.run(inputs + ["-t", f"{dur:.2f}", "-filter_complex", graph,
                             "-map", label, "-map", "0:a?",
                             "-c:v", "libx264", "-preset", "veryfast",
                             "-crf", "21", "-maxrate", str(rate), "-bufsize", str(rate * 2),
                             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                             "-movflags", "+faststart", out])
            if os.path.getsize(out) <= cap:
                break
    finally:
        if ass and os.path.exists(ass):
            os.remove(ass)
    return os.path.getsize(out)
