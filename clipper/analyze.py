from __future__ import annotations
import wave
import numpy as np
from . import ff
from .config import Config


def extract_audio(video: str, wav: str):
    ff.run(["-i", video, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", wav])


def audio_energy(wav: str, chunk_s: int = 600) -> np.ndarray:
    """Per-second RMS in dB, read in chunks so multi-hour files stay cheap on memory."""
    out = []
    with wave.open(wav, "rb") as w:
        sr = w.getframerate()
        while True:
            raw = w.readframes(sr * chunk_s)
            if not raw:
                break
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            n = len(x) // sr
            if n == 0:
                break
            x = x[: n * sr].reshape(n, sr)
            out.append(20 * np.log10(np.sqrt((x ** 2).mean(axis=1)) + 1e-6))
    return np.concatenate(out) if out else np.zeros(1)


def _clip_reach(cfg: Config):
    """Seconds a clip can extend before/after its peak (see pick_clips/_snap)."""
    lead = cfg.target_len * 0.6
    return lead + 5, cfg.max_len - lead + 5


def candidate_windows(energy: np.ndarray, total: float, cfg: Config):
    """Loudest stretches of the video, as merged (start, end) windows worth transcribing.

    Cheap pre-pass so Whisper only runs where a clip could land.
    """
    n = len(energy)
    audio = np.convolve(np.clip(_z(energy), 0, None), np.ones(8) / 8, mode="same")
    before, after = _clip_reach(cfg)
    pad = 5.0  # extra context so peaks can shift once speech is scored
    want = max(1, int(cfg.max_clips * cfg.candidates_per_clip))
    sc = audio.copy()
    wins = []
    while len(wins) < want and sc.max() > 0:
        t = int(sc.argmax())
        wins.append((max(0.0, t - before - pad), min(total, t + after + pad)))
        sc[max(0, int(t - before - after)):min(n, int(t + before + after))] = 0
    wins.sort()
    merged = []
    for a, b in wins:
        if merged and a <= merged[-1][1] + 5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def peak_mask(n: int, windows, total: float, cfg: Config) -> np.ndarray:
    """1 where a clip peak keeps the whole clip inside a transcribed window."""
    before, after = _clip_reach(cfg)
    m = np.zeros(n)
    for a, b in windows:
        lo = 0 if a <= 0 else int(np.ceil(a + before))
        hi = n if b >= total else int(b - after)
        if hi > lo:
            m[lo:hi] = 1
    return m


_MODELS = {}


def _model(cfg: Config):
    """Load each Whisper model once per process (live mode transcribes every minute)."""
    key = (cfg.whisper_model, cfg.device, cfg.compute_type)
    if key not in _MODELS:
        from faster_whisper import WhisperModel
        _MODELS[key] = WhisperModel(cfg.whisper_model, device=cfg.device,
                                    compute_type=cfg.compute_type)
    return _MODELS[key]


def transcribe(wav: str, cfg: Config, windows=None):
    """Return (segments, language). Segment: dict(start, end, text, words=[(s, e, w)]).

    windows: list of (start, end) seconds to transcribe; None = whole file.
    """
    model = _model(cfg)
    # Pass samples directly: our WAV is already 16 kHz mono, so skip PyAV decoding.
    with wave.open(wav, "rb") as w:
        sr = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = audio.astype(np.float32) / 32768.0
    if windows is None:
        windows = [(0.0, len(audio) / sr)]
    lang = cfg.language
    res = []
    for a, b in windows:
        part = audio[int(a * sr):int(b * sr)]
        if len(part) < sr:
            continue
        segs, info = model.transcribe(part, language=lang, word_timestamps=True,
                                      vad_filter=True)
        for s in segs:  # generator: streams through long audio
            res.append({"start": s.start + a, "end": s.end + a, "text": s.text.strip(),
                        "words": [(w.start + a, w.end + a, w.word) for w in (s.words or [])]})
        lang = lang or info.language  # detect once, reuse for later windows
    return res, lang


def _z(a: np.ndarray) -> np.ndarray:
    sd = a.std()
    return (a - np.median(a)) / (sd if sd > 1e-6 else 1.0)


def score_seconds(n: int, energy: np.ndarray, segs, cfg: Config, baseline=None) -> np.ndarray:
    """Per-second score: loudness spikes + keyword hits + speech density.

    baseline: (median_db, std_db) of the whole stream so far. Live mode passes it so a quiet
    chunk is not judged only against itself.
    """
    e = np.full(n, float(np.median(energy)))
    m = min(n, len(energy))
    e[:m] = energy[:m]
    audio = np.clip(_z(e) if baseline is None else (e - baseline[0]) / max(baseline[1], 1e-6),
                    0, None)

    kw = np.zeros(n)
    sp = np.zeros(n)
    for s in segs:
        low = s["text"].lower()
        hits = sum(low.count(k) for k in cfg.keywords)
        a, b = int(s["start"]), min(n, int(s["end"]) + 1)
        if b > a:
            kw[a:b] += hits / (b - a)
            sp[a:b] += len(s["words"]) / (b - a)
    sp = np.clip(_z(sp), 0, None)
    kw = kw / (kw.max() + 1e-6) * 3
    sc = cfg.w_audio * audio + cfg.w_keywords * kw + cfg.w_speech * sp
    k = np.ones(8) / 8  # smooth ~8s: score moments, not single loud frames
    return np.convolve(sc, k, mode="same")


def pick_clips(score: np.ndarray, segs, total: float, cfg: Config):
    """Greedy non-overlapping peaks -> sorted [(start, end, score)] within [min_len, max_len]."""
    if total < cfg.min_len:
        return []
    sc = score.copy()
    picks = []
    lead = cfg.target_len * 0.6  # peak sits ~60% into the clip
    while len(picks) < cfg.max_clips and sc.max() > 0:
        t = int(sc.argmax())
        val = float(sc[t])
        end = min(total, max(0.0, t - lead) + cfg.target_len)
        start = max(0.0, end - cfg.target_len)
        start, end = _snap(start, end, segs, cfg, total)
        if cfg.min_len <= end - start <= cfg.max_len + 1e-6:
            picks.append((start, end, val))
        a = max(0, int(start - cfg.min_gap))
        b = min(len(sc), int(end + cfg.min_gap))
        sc[a:b] = 0
    return sorted(picks)


def _snap(start, end, segs, cfg, total):
    """Nudge edges to sentence boundaries (<=4s), then enforce [min_len, max_len]."""
    for s in segs:
        if s["start"] < start < s["end"] and s["start"] >= start - 4:
            start = s["start"]
            break
    for s in segs:
        if s["start"] < end < s["end"] and s["end"] <= end + 4:
            end = s["end"]
            break
    end = min(end, total)
    if end - start > cfg.max_len:
        end = start + cfg.max_len
    if end - start < cfg.min_len:
        end = min(total, start + cfg.min_len)
    if end - start < cfg.min_len:
        start = max(0.0, end - cfg.min_len)
    return start, end
