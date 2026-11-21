from __future__ import annotations
import json
import os
import time
import random
from . import ff, source, analyze, export, facecam, sticker, profiles
from .config import Config


def title_from(segs, a, b) -> str:
    txt = [s["text"] for s in segs if s["start"] >= a and s["end"] <= b and len(s["text"]) > 12]
    if not txt:
        return "Highlight"
    best = max(txt, key=len)
    return (best[:70] + "…") if len(best) > 70 else best


def find_facecam(video, start, end, src_w, src_h, cfg: Config, n=40, min_ratio=0.5):
    """Detect the webcam in [start, end] (manual --facecam wins). None if not found."""
    if cfg.facecam_rect:
        return tuple(cfg.facecam_rect)
    if cfg.layout not in ("auto", "facecam"):
        return None
    aspect = cfg.width / (int(cfg.height * cfg.facecam_frac) // 2 * 2)
    return facecam.detect(video, start, end, src_w, src_h, aspect, n=n, min_ratio=min_ratio)


def resolve_facecam(video, total, src_w, src_h, cfg: Config, cid, name, log, outdir=None):
    """Webcam rect for this video: --facecam > confirmed per-channel rect > detection
    (which is then remembered for the channel) > previously auto-detected rect."""
    if cfg.facecam_rect:
        return tuple(cfg.facecam_rect)
    if cfg.layout not in ("auto", "facecam"):
        return None
    saved, confirmed = profiles.get(cid, (src_w, src_h))
    if confirmed:
        log(f"facecam: {saved if saved != profiles.NONE else 'none'} (confirmed for {name})")
        return None if saved == profiles.NONE else saved
    cam = find_facecam(video, 0, total, src_w, src_h, cfg)
    if cam:
        profiles.put(cid, name, cam, size=(src_w, src_h))
    elif saved and saved != profiles.NONE:
        cam = saved
        log("facecam: not detected this time, using the one saved for this channel")
    if cam and outdir:
        prev = os.path.join(outdir, "facecam_preview.jpg")
        try:
            profiles.preview(video, total / 2, cam, prev)
        except RuntimeError:
            prev = None
    log(f"facecam: {cam if cam else 'not found (webcam must be visible most of the time)'}"
        + (f"  -> check {outdir}/facecam_preview.jpg" if cam and outdir else ""))
    return cam


def choose_layout(video, a, b, src_w, src_h, fillers, cfg: Config, log, cam=None):
    """Pick the layout for one clip. Returns (layout, facecam_rect, filler_path).

    cam: webcam rect found for the whole video (see find_facecam), or None.
    """
    want = cfg.layout
    if want in ("blur", "crop"):
        return want, None, None
    if want in ("auto", "facecam"):
        if cam:
            return "facecam", tuple(cam), None
        if want == "facecam":
            log("    no facecam found, falling back")
    if fillers:
        return "filler", None, random.choice(fillers)
    if want == "filler":
        log(f"    no filler videos in {cfg.filler_dir}, using blur")
    return "blur", None, None


def process(src: str, outdir: str, cfg: Config, log=print, sections=None):
    """Clip a video. sections=[(start, end), ...] seconds limits download + search to those parts.

    --max-clips applies per section. Clip names/timestamps use the original video's times.
    """
    os.makedirs(outdir, exist_ok=True)
    work = os.path.join(outdir, "_work")
    os.makedirs(work, exist_ok=True)
    if cfg.sticker:
        sticker.ensure(cfg.sticker)
    results, lang = [], None
    for sec in (sections or [None]):
        if sec:
            log(f"section {source._hms(sec[0])} - {source._hms(sec[1])}: fetching…")
        else:
            log("fetching source…")
        video = source.fetch(src, work, sec)
        offset = sec[0] if sec else 0.0
        res, lang = _process_video(video, outdir, work, cfg, log, offset, len(results), src)
        results += res
    with open(os.path.join(outdir, "clips.json"), "w", encoding="utf-8") as f:
        json.dump({"source": src, "sections": sections, "language": lang, "clips": results}, f,
                  ensure_ascii=False, indent=2)
    return results


def _process_video(video, outdir, work, cfg: Config, log, offset: float, numbered: int, src=""):
    total = ff.duration(video)
    log(f"video: {video} ({total / 60:.1f} min)")
    wav = os.path.join(work, "audio.wav")
    log("extracting audio…"); analyze.extract_audio(video, wav)
    log("measuring loudness…"); energy = analyze.audio_energy(wav)
    n = int(total) + 1
    windows = None
    if not cfg.full_transcribe:
        windows = analyze.candidate_windows(energy, total, cfg)
        covered = sum(b - a for a, b in windows)
        if covered >= 0.8 * total:
            windows = None  # nearly everything anyway: one pass is simpler
        else:
            log(f"candidates: {len(windows)} windows, {covered / 60:.1f} of {total / 60:.1f} min")
    log(f"transcribing (whisper {cfg.whisper_model} on {cfg.device})…")
    t0 = time.time()
    segs, lang = analyze.transcribe(wav, cfg, windows)
    log(f"language={lang}, {len(segs)} segments, {time.time() - t0:.0f}s")
    sc = analyze.score_seconds(n, energy, segs, cfg)
    if windows is not None:
        sc = sc * analyze.peak_mask(n, windows, total, cfg)
    picks = analyze.pick_clips(sc, segs, total, cfg)
    log(f"selected {len(picks)} moments")
    src_w, src_h = ff.video_size(video)
    fillers = export.list_fillers(cfg)
    cam = None
    if picks:
        cid, name = profiles.channel(src)
        cam = resolve_facecam(video, total, src_w, src_h, cfg, cid, name, log, outdir)
    results = []
    for i, (a, b, val) in enumerate(picks, numbered + 1):
        ta = offset + a  # time in the original video
        name = (f"clip_{i:02d}_score{val:.2f}_"
                f"{int(ta) // 3600}h{int(ta) % 3600 // 60:02d}m{int(ta) % 60:02d}s.mp4")
        path = os.path.join(outdir, name)
        words = [w for s in segs for w in s["words"] if a <= w[0] < b]
        layout, rect, fil = choose_layout(video, a, b, src_w, src_h, fillers, cfg, log, cam)
        size = export.export_clip(video, a, b, words, path, cfg, layout, rect, fil)
        meta = {"file": name, "start": round(ta, 2), "end": round(offset + b, 2),
                "duration": round(b - a, 2), "score": round(val, 3),
                "size_mb": round(size / 1048576, 2), "layout": layout,
                "facecam": list(rect) if rect else None,
                "filler": os.path.basename(fil) if fil else None,
                "title": title_from(segs, a, b)}
        results.append(meta)
        log(f"  {name}  {meta['duration']}s  {meta['size_mb']}MB  score={meta['score']}  layout={layout}")
    return results, lang
