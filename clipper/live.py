"""Live mode: record streams continuously and cut clips while they are still running."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
import numpy as np
from . import analyze, export, ff, pipeline, profiles, sticker
from .config import Config
from .recorder import Recorder


def name_from_url(url: str) -> str:
    for pat in (r"youtube\.com/@([\w.-]+)", r"youtube\.com/(?:c|channel|user)/([\w.-]+)",
                r"[?&]v=([\w-]{6,})", r"youtu\.be/([\w-]{6,})", r"kick\.com/([\w.-]+)"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return re.sub(r"\W+", "_", url)[-40:]


class Job:
    def __init__(self, url: str, root: str, log_fn):
        self.url = url
        self.name = name_from_url(url)
        self.dir = os.path.join(root, self.name)
        self.log = lambda msg: log_fn(f"[{self.name}] {msg}")
        self.rec = Recorder(url, os.path.join(self.dir, "_segments"), self.log)
        self.energy = np.zeros(0)       # per-second loudness (dB) over the whole stream
        self.analyzed_until = 0.0       # stream seconds already searched for peaks
        self.block_start = None         # start of the current "at least one clip" block
        self.best_seen = 0.0            # best audio score since last status line
        self.block_best = None          # stream second of the open block's best moment (kept on disk)
        self.tail_cut = 0.0             # pieces before this stream time may already be deleted
        self.cam = None                 # webcam rect, confirmed by two agreeing detections
        self.cid = self.cname = None    # channel id/name (for facecams.json)
        self.cam_pending = None
        self.clips = []                 # (start, end) in stream seconds
        self.meta = []
        self.t0_wall = None             # wall clock at stream time 0
        self.counter = 0
        os.makedirs(self.dir, exist_ok=True)
        meta_path = os.path.join(self.dir, "clips.json")
        if os.path.exists(meta_path):   # keep numbering/history across restarts of live.py
            with open(meta_path, encoding="utf-8") as f:
                self.meta = json.load(f).get("clips", [])
            self.counter = len(self.meta)


def _build_chunk(rec: Recorder, t0: float, t1: float, out: str):
    """Mux recorded segments overlapping [t0, t1] into one .ts. Returns its stream start time."""
    segs = rec.segments(t0, t1)
    if not segs:
        return None
    vcat, acat = out + ".v.ts", out + ".a.ts"
    have_audio_track = rec.paths(segs[0][2])[1] is not None
    with open(vcat, "wb") as fv, open(acat, "wb") as fa:
        for _, _, key in segs:
            v, a = rec.paths(key)
            with open(v, "rb") as f:
                shutil.copyfileobj(f, fv)
            if a:
                with open(a, "rb") as f:
                    shutil.copyfileobj(f, fa)
    if have_audio_track:
        ff.run(["-i", vcat, "-i", acat, "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", out])
    else:
        ff.run(["-i", vcat, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", out])
    for p in (vcat, acat):
        os.remove(p)
    return segs[0][0]


class LiveClipper:
    def __init__(self, urls, root: str, cfg: Config, min_score: float, per_hour: int,
                 warmup: float = 180.0, floor: float = 0.3, log=print):
        self.cfg, self.root = cfg, root
        self.min_score, self.per_hour, self.warmup = min_score, per_hour, warmup
        self.floor = floor                      # weakest moment the per-block pick may use
        self.block = 3600.0 / max(1, per_hour)  # e.g. 8/hour -> best moment every 7.5 min
        self.print = log
        self.jobs = [Job(u, root, self._log) for u in urls]
        self.stop_flag = threading.Event()
        self.fillers = export.list_fillers(cfg)
        self.before, self.after = analyze._clip_reach(cfg)

    def _log(self, msg):
        self.print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)

    # ---- main loop -------------------------------------------------------------------
    def run(self):
        if self.cfg.sticker:
            sticker.ensure(self.cfg.sticker)
        for j in self.jobs:
            j.rec.start()
        last_status = 0.0
        try:
            while not self.stop_flag.is_set():
                for j in self.jobs:
                    try:
                        self._step(j)
                    except Exception as e:
                        j.log(f"analysis error: {type(e).__name__}: {e}")
                if time.time() - last_status > 120:
                    self._status()
                    last_status = time.time()
                self.stop_flag.wait(15)
        except KeyboardInterrupt:
            pass
        finally:
            self._log("stopping recorders…")
            for j in self.jobs:
                j.rec.stop()

    def _status(self):
        for j in self.jobs:
            state = "LIVE" if j.rec.live else "offline"
            j.log(f"{state}, recorded {j.rec.duration() / 60:.1f} min, "
                  f"analyzed {j.analyzed_until / 60:.1f} min, clips {j.counter}, "
                  f"best moment since last update {j.best_seen:.2f}")
            j.best_seen = 0.0

    # ---- per-stream work ---------------------------------------------------------------
    def _step(self, j: Job):
        D = j.rec.duration()
        if D <= 0:
            return
        if j.t0_wall is None:
            j.t0_wall = time.time() - D
        self._update_energy(j, D)
        n = len(j.energy)
        if n < self.warmup:
            return
        lo, hi = j.analyzed_until, n - self.after - 2
        if hi - lo < 20:
            return
        recent = j.energy[max(0, n - 1800):]   # last 30 min: adapts to volume changes
        base = (float(np.median(recent)), float(recent.std()) or 1.0)
        audio = np.clip((j.energy - base[0]) / base[1], 0, None)
        audio = np.convolve(audio, np.ones(8) / 8, mode="same") * self.cfg.w_audio
        if j.block_start is None:
            j.block_start = lo
        # 1) strong moments: clip right away
        sc = audio.copy()
        sc[: int(lo)] = 0
        sc[int(hi):] = 0
        j.best_seen = max(j.best_seen, float(sc.max()))
        gate = self.min_score * 0.75            # cheap audio-only pre-filter
        while sc.max() >= gate:
            t = int(sc.argmax())
            sc[max(0, int(t - self.before - self.after)):int(t + self.before + self.after)] = 0
            if not self._near_existing(j, t):
                self._try_clip(j, t, base, self.min_score)
        j.analyzed_until = hi
        # 2) steady output: if a whole block passed with no clip, take its best moment
        while hi - j.block_start >= self.block:
            b0, b1 = j.block_start, j.block_start + self.block
            if not any(b0 <= a < b1 for a, _ in j.clips):
                blk = audio.copy()
                blk[: int(b0)] = 0
                blk[int(b1):] = 0
                for a, b in j.clips:
                    blk[max(0, int(a - self.cfg.min_gap)):int(b + self.cfg.min_gap)] = 0
                # only the remembered best moment is guaranteed to still be on disk
                t = j.block_best if j.block_best is not None and b0 <= j.block_best < b1 else None
                if t is not None and blk[t] >= self.floor * self.cfg.w_audio:
                    j.log(f"no strong moment in {b0 / 60:.1f}-{b1 / 60:.1f} min, "
                          f"using its best (audio {blk[t]:.2f})")
                    self._try_clip(j, t, base, self.floor)
                else:
                    j.log(f"{b0 / 60:.1f}-{b1 / 60:.1f} min too quiet, no clip")
            j.block_start = b1
            j.block_best = None
        # Disk: keep only the recent tail (for moments still being scored) plus the pieces
        # around the open block's best moment so far (for the fallback clip). Drop the rest.
        keep = []
        if not any(j.block_start <= a < hi for a, _ in j.clips):
            blk = audio.copy()
            blk[: int(j.block_start)] = 0
            blk[int(hi):] = 0
            for a, b in j.clips:
                blk[max(0, int(a - self.cfg.min_gap)):int(b + self.cfg.min_gap)] = 0
            # new candidates must still be fully on disk (after the previous tail cut)
            fresh = blk.copy()
            fresh[: max(0, int(np.ceil(j.tail_cut + self.before + 10)))] = 0
            t = int(fresh.argmax()) if fresh.max() > 0 else None
            if j.block_best is not None and (t is None or blk[j.block_best] >= fresh[t]):
                t = j.block_best
            if t is not None:
                j.block_best = t
                keep = [(t - self.before - 10, t + self.after + 10)]
        j.tail_cut = hi - self.before - 15
        j.rec.drop_before(j.tail_cut, keep)

    def _update_energy(self, j: Job, D: float):
        start = float(len(j.energy))
        if D - start < 10:
            return
        work = os.path.join(j.dir, "_work")
        os.makedirs(work, exist_ok=True)
        chunk = os.path.join(work, "energy.ts")
        c0 = _build_chunk(j.rec, start, D, chunk)
        if c0 is None:
            return
        wav = os.path.join(work, "energy.wav")
        ff.run(["-ss", f"{start - c0:.3f}", "-i", chunk, "-t", f"{D - start:.3f}",
                "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", wav])
        e = analyze.audio_energy(wav)
        j.energy = np.concatenate([j.energy, e])
        os.remove(chunk)
        os.remove(wav)

    def _facecam(self, j: Job, chunk, total, sw, shh):
        """Webcam rect for this stream: --facecam > confirmed per-channel rect > detection.
        Detection uses each clip chunk (~70s) with a strict ratio and is only locked once two
        chunks agree, so a short alert/super-chat GIF can't win. The locked rect is remembered
        for the channel, and a preview image is saved in the stream's folder."""
        if self.cfg.facecam_rect:
            return self.cfg.facecam_rect
        if j.cam:
            return None if j.cam == profiles.NONE else j.cam
        if j.cid is None:
            j.cid, j.cname = profiles.channel(j.url)
        saved, confirmed = profiles.get(j.cid, (sw, shh))
        if confirmed:
            j.cam = saved
            j.log(f"facecam: using confirmed {saved} for {j.cname}")
            return None if saved == profiles.NONE else saved
        cam = pipeline.find_facecam(chunk, 0, total, sw, shh, self.cfg, n=20, min_ratio=0.75)
        if not cam:
            return saved if saved and saved != profiles.NONE else None
        p = j.cam_pending
        if p and abs(p[0] - cam[0]) < sw * 0.03 and abs(p[1] - cam[1]) < shh * 0.03 \
                and abs(p[2] - cam[2]) < sw * 0.05:
            j.cam = cam
            profiles.put(j.cid, j.cname, cam, size=(sw, shh))
            prev = os.path.join(j.dir, "facecam_preview.jpg")
            try:
                profiles.preview(chunk, total / 2, cam, prev)
            except RuntimeError:
                pass
            j.log(f"facecam locked at {cam}  -> check {prev}")
        j.cam_pending = cam
        return cam

    def _near_existing(self, j: Job, t: float) -> bool:
        return any(a - self.cfg.min_gap < t < b + self.cfg.min_gap for a, b in j.clips)

    def _rate_ok(self, j: Job) -> bool:
        cutoff = time.time() - 3600
        return sum(1 for m in j.meta if m.get("created", 0) > cutoff) < self.per_hour

    def _try_clip(self, j: Job, t: int, base, threshold: float):
        work = os.path.join(j.dir, "_work")
        try:
            self._clip(j, t, base, threshold, work)
        finally:
            for f in ("clip_src.ts", "clip_src.wav"):
                p = os.path.join(work, f)
                if os.path.exists(p):
                    os.remove(p)

    def _clip(self, j: Job, t: int, base, threshold: float, work: str):
        cfg = self.cfg
        if not self._rate_ok(j):
            j.log(f"peak at {t / 60:.1f} min skipped: {self.per_hour}/hour limit reached")
            return
        w0, w1 = max(0.0, t - self.before - 5), t + self.after + 5
        chunk = os.path.join(work, "clip_src.ts")
        c0 = _build_chunk(j.rec, w0, w1, chunk)
        if c0 is None:
            return
        total = ff.duration(chunk)
        wav = os.path.join(work, "clip_src.wav")
        analyze.extract_audio(chunk, wav)
        segs, lang = analyze.transcribe(wav, cfg)
        if cfg.language is None and lang:
            cfg.language = lang                 # detect once per run, then reuse
        # score in chunk-local time using the stream baseline
        e = j.energy[int(c0):int(c0) + int(total) + 1]
        local = analyze.score_seconds(int(total) + 1, e, segs, cfg, baseline=base)
        one = Config(**{**cfg.__dict__, "max_clips": 1})
        picks = analyze.pick_clips(local, segs, total, one)
        if not picks:
            return
        a, b, val = picks[0]
        if val < threshold:
            j.log(f"peak at {t / 60:.1f} min score {val:.2f} < {threshold}, skipped")
            return
        sa, sb = c0 + a, c0 + b
        if any(x - cfg.min_gap < sb and sa < y + cfg.min_gap for x, y in j.clips):
            return
        j.counter += 1
        when = datetime.fromtimestamp((j.t0_wall or time.time()) + sa)
        name = f"clip_{j.counter:03d}_score{val:.2f}_{when:%Y%m%d_%H%M%S}.mp4"
        path = os.path.join(j.dir, name)
        sw, shh = ff.video_size(chunk)
        layout, cam, fil = pipeline.choose_layout(chunk, a, b, sw, shh, self.fillers, cfg, j.log,
                                                  self._facecam(j, chunk, total, sw, shh))
        words = [w for s in segs for w in s["words"] if a <= w[0] < b]
        size = export.export_clip(chunk, a, b, words, path, cfg, layout, cam, fil)
        j.clips.append((sa, sb))
        j.meta.append({"file": name, "stream_time": round(sa, 1), "duration": round(b - a, 2),
                       "score": round(val, 3), "size_mb": round(size / 1048576, 2),
                       "layout": layout, "facecam": list(cam) if cam else None,
                       "title": pipeline.title_from(segs, a, b), "created": time.time(),
                       "wall_time": when.isoformat(timespec="seconds")})
        with open(os.path.join(j.dir, "clips.json"), "w", encoding="utf-8") as f:
            json.dump({"source": j.url, "clips": j.meta}, f, ensure_ascii=False, indent=2)
        j.log(f"CLIP {name}  {b - a:.1f}s  {size / 1048576:.1f}MB  score={val:.2f}  layout={layout}")
