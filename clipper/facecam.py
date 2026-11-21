"""Find a streamer's webcam overlay: a face that stays in the same place across frames."""
from __future__ import annotations
import os
import subprocess
from typing import Optional, Tuple
import numpy as np
from . import ff

MODEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "models", "face_detection_yunet_2023mar.onnx")
SCAN_W = 1280   # detect on downscaled frames (small webcams need the resolution)
MIN_FACE = 0.012  # ignore faces narrower than this share of frame width


def _frames(video: str, start: float, end: float, n: int):
    import cv2
    out = []
    for i in range(n):
        t = start + (end - start) * (i + 0.5) / n
        p = subprocess.run([ff.FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}",
                            "-i", video, "-frames:v", "1", "-vf", f"scale={SCAN_W}:-2",
                            "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
        if p.stdout:
            img = cv2.imdecode(np.frombuffer(p.stdout, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                out.append(img)
    return out


def _faces(frames, det):
    """-> [(frame_idx, cx, cy, w, h)] in scan pixels."""
    fh, fw = frames[0].shape[:2]
    hits = []
    for i, img in enumerate(frames):
        if img.shape[:2] != (fh, fw):
            continue
        _, faces = det.detect(img)
        if faces is None:
            continue
        for f in faces:
            x, y, w, h = f[:4]
            if w >= fw * MIN_FACE:
                hits.append((i, x + w / 2, y + h / 2, w, h))
    return hits


def _stable_cluster(hits, fw, nframes, min_ratio):
    """Face position seen in the most frames. Webcams persist; alerts and game faces don't."""
    tol = fw * 0.04
    best, best_n = [], 0
    for _, cx, cy, _, _ in hits:
        grp = [h for h in hits if abs(h[1] - cx) < tol and abs(h[2] - cy) < tol]
        n = len({g[0] for g in grp})
        if n > best_n:
            best, best_n = grp, n
    if best_n < max(3, min_ratio * nframes):
        return None
    return best


def detect(video: str, start: float, end: float, src_w: int, src_h: int,
           out_aspect: float, n: int = 12, min_ratio: float = 0.4) -> Optional[Tuple[int, int, int, int]]:
    """Return crop rect (x, y, w, h) in source pixels around a stable face, or None.

    Sample n frames across [start, end]. For whole videos pass the full range with a larger n
    (e.g. 40) and min_ratio ~0.5: a real webcam is in almost every frame, while super-chat /
    membership alert GIFs and in-game faces only appear for a few seconds.
    out_aspect = width/height of the facecam panel in the output.
    """
    import cv2
    frames = _frames(video, start, end, n)
    if not frames or not os.path.exists(MODEL):
        return None
    fh, fw = frames[0].shape[:2]
    det = cv2.FaceDetectorYN.create(MODEL, "", (fw, fh), 0.7)
    best = _stable_cluster(_faces(frames, det), fw, len(frames), min_ratio)
    if not best:
        return None
    cx, cy, w, h = (float(np.median([g[k] for g in best])) for k in (1, 2, 3, 4))
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames
             if f.shape[:2] == (fh, fw)]
    bx0, by0, bx1, by1 = _overlay_box(grays, cx, cy, w, h, fw, fh)
    s = src_w / fw
    even = lambda v: int(round(v * s)) // 2 * 2
    x0, y0 = even(bx0), even(by0)
    x1, y1 = min(src_w, even(bx1)), min(src_h, even(by1))
    return x0, y0, x1 - x0, y1 - y0   # the whole webcam overlay; export fits it in the panel


def _edge(profile: np.ndarray, lo: int, hi: int, fallback: float) -> float:
    """Strongest persistent edge position in profile[lo:hi], else fallback."""
    lo, hi = max(0, lo), min(len(profile), hi)
    if hi - lo < 3:
        return fallback
    seg = profile[lo:hi]
    i = int(seg.argmax())
    if seg[i] > max(4.0, 4 * float(np.median(profile))):
        return float(lo + i)
    return fallback


def _peaks(cov: np.ndarray, start: int, stop: int, thr: float = 0.35, k: int = 6):
    """Candidate border positions between start and stop: local maxima of line coverage."""
    lo, hi = sorted((start, stop))
    lo, hi = max(1, lo), min(len(cov) - 1, hi)
    c = [i for i in range(lo, hi) if cov[i] >= thr and cov[i] >= cov[i - 1] and cov[i] >= cov[i + 1]]
    return sorted(c, key=lambda i: -cov[i])[:k]


def _overlay_box(grays, cx, cy, w, h, fw, fh):
    """Find the webcam overlay borders around a face (in scan-frame pixels).

    Overlay borders are straight lines that stay put while gameplay changes: take a low
    percentile of gradient strength across frames. Collect candidate lines on each side of
    the face, then pick the rectangle whose four sides are most complete lines, with a
    webcam-like shape (landscape-ish, face a sensible share of the width). Static edges inside
    the webcam or in the game (shelves, doors, letterbox bars) rarely form such a rectangle.
    """
    gx = np.percentile([np.abs(np.diff(g, axis=1)) for g in grays], 25, axis=0) > 3.0
    gy = np.percentile([np.abs(np.diff(g, axis=0)) for g in grays], 25, axis=0) > 3.0
    lim = 6.0
    # rough coverage profiles over a band around the face
    by0, by1 = int(max(0, cy - 1.2 * h)), int(min(fh - 1, cy + 1.2 * h))
    bx0, bx1 = int(max(0, cx - 1.5 * w)), int(min(fw - 1, cx + 1.5 * w))
    col = gx[by0:by1].mean(axis=0)
    row = gy[:, bx0:bx1].mean(axis=1)
    L = _peaks(col, int(cx - 0.55 * w), int(cx - lim * w)) + [0]
    R = _peaks(col, int(cx + 0.55 * w), int(cx + lim * w)) + [fw - 1]
    T = _peaks(row, int(cy - 0.55 * h), int(cy - lim * h)) + [0]
    B = _peaks(row, int(cy + 0.55 * h), int(cy + lim * h)) + [fh - 1]

    def side(a, b0, b1, vertical):
        if b1 - b0 < 4:
            return 0.0
        if a <= 0 or a >= (fw - 1 if vertical else fh - 1):
            return 0.9  # frame border: overlay touches the screen edge
        return float((gx[b0:b1, a] if vertical else gy[a, b0:b1]).mean())

    best, best_s = None, -1.0
    for x0 in L:
        for x1 in R:
            bw = x1 - x0
            if not 0.12 <= w / bw <= 0.6:
                continue
            for y0 in T:
                for y1 in B:
                    bh = y1 - y0
                    if not 1.0 <= bw / bh <= 2.4 or not 0.15 <= h / bh <= 0.75:
                        continue
                    sc = (side(x0, y0, y1, True) + side(x1, y0, y1, True)
                          + side(y0, x0, x1, False) + side(y1, x0, x1, False)) / 4
                    sc -= 0.05 * (bw * bh) / (fw * fh)  # mild preference for the tighter box
                    if sc > best_s:
                        best, best_s = (x0, y0, x1 + 1, y1 + 1), sc
    if best is None or best_s < 0.45:
        # borderless webcam: a moderate box around the face
        bw = min(fw, 3.4 * w); bh = min(fh, bw / 1.6)
        x0 = min(max(0.0, cx - bw / 2), fw - bw); y0 = min(max(0.0, cy - bh * 0.45), fh - bh)
        return x0, y0, x0 + bw, y0 + bh
    x0, y0, x1, y1 = best
    return float(x0), float(y0), float(min(fw, x1)), float(min(fh, y1))
